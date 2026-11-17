from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import translation

from pyracast.accounts.models import UserLanguagePreference
from pyracast.forecasts.models import ForecastSnapshot, RequestAttempt
from pyracast.mail.errors import NotSentError
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.mail import build_message
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.notifications.scheduler import enqueue_due
from pyracast.regions.models import Region
from tests.test_domain import NOW, System
from tests.test_notification_preparation import complete_saved
from tests.test_notifications import SLOT, attached_sheet, configured

pytestmark = pytest.mark.django_db(transaction=True)


def pinned_deliveries(
    system: System, schedule: ReminderSchedule, language: str, recipients: list[str]
) -> list[NotificationDelivery]:
    schedule.recipients = list(dict.fromkeys([*schedule.recipients, *recipients]))
    schedule.save(update_fields=["recipients"])
    snapshot = (
        ForecastSnapshot.objects.filter(binding=system.binding)
        .order_by("-collected_at", "-pk")
        .first()
    )
    return [
        NotificationDelivery.objects.create(
            schedule=schedule,
            scheduled_for=SLOT,
            available_at=SLOT,
            binding=system.binding,
            snapshot=snapshot,
            preparation_deadline=SLOT + timedelta(minutes=15),
            recipients=[recipient],
            grouped=False,
            language=language,
        )
        for recipient in recipients
    ]


@pytest.mark.parametrize("language", [None, "en", "tr"])
def test_schedule_pins_explicit_language_for_every_recipient_and_defaults_to_english(
    language: str | None,
) -> None:
    system, schedule = configured()
    recipient_user = User.objects.create(username="recipient", email="recipient@example.com")
    UserLanguagePreference.objects.create(user=recipient_user, language="tr")
    schedule.recipients = [recipient_user.email, "external@example.com"]
    if language is not None:
        schedule.language = language
    schedule.save()
    UserLanguagePreference.objects.create(
        user=system.member, language="tr" if language != "tr" else "en"
    )
    with translation.override("tr" if language != "tr" else "en"):
        ids = enqueue_due(SLOT)
    assert len(ids) == 1
    assert set(NotificationDelivery.objects.values_list("language", flat=True)) == {
        language or "en"
    }
    UserLanguagePreference.objects.update_or_create(
        user=system.member, defaults={"language": language or "en"}
    )
    schedule.language = "tr" if language != "tr" else "en"
    schedule.save(update_fields=["language"])
    assert sorted(enqueue_due(SLOT)) == sorted(ids)
    assert set(NotificationDelivery.objects.values_list("language", flat=True)) == {
        language or "en"
    }


@pytest.mark.parametrize("first_language", ["en", "tr"])
def test_mixed_languages_share_one_frozen_source_and_retries_keep_original_content(
    first_language: str,
) -> None:
    system, schedule = configured()
    snapshot = complete_saved(system)
    requests = RequestAttempt.objects.count()
    enqueue_due(SLOT)
    pinned_deliveries(system, schedule, "tr", ["other@example.com", "external@example.com"])
    first = NotificationDelivery.objects.filter(language=first_language).first()
    assert first is not None
    messages: list[EmailMultiAlternatives] = []

    def temporary_failure(message: EmailMultiAlternatives) -> None:
        messages.append(message)
        raise NotSentError

    assert not process_notification(first.pk, SLOT, sender=temporary_failure)
    first.refresh_from_db()
    deliveries = list(NotificationDelivery.objects.all())
    assert len(deliveries) == 3
    assert {delivery.snapshot_id for delivery in deliveries} == {snapshot.pk}
    payloads = {delivery.pk: delivery.prepared_message for delivery in deliveries}
    assert all(isinstance(payload, dict) for payload in payloads.values())
    english = next(
        delivery.prepared_message for delivery in deliveries if delivery.language == "en"
    )
    turkish, repeated_turkish = (
        delivery.prepared_message for delivery in deliveries if delivery.language == "tr"
    )
    assert english is not None
    assert turkish is not None
    assert turkish == repeated_turkish
    assert english["prepared_at"] == turkish["prepared_at"]
    assert english["kind"] == turkish["kind"] == "saved_complete"
    assert english["report_context"] == turkish["report_context"]
    assert english["results"] == turkish["results"]
    sheets = [attached_sheet(build_message(record)) for record in deliveries]
    numeric_values = [
        [cell.value for row in sheet for cell in row if isinstance(cell.value, int | float)]
        for sheet in sheets
    ]
    for sheet in sheets:
        for day in (10, 11):
            row = next(cell.row for cell in sheet["A"] if cell.value == datetime(2026, 11, day))
            assert isinstance(sheet.cell(row, 3).value, int | float)
    assert numeric_values[0]
    assert numeric_values[0] == numeric_values[1] == numeric_values[2]
    assert "3.299 kWh/m²" in english["body"]
    assert "3,299 kWh/m²" in turkish["body"]
    assert '<html lang="en">' in english["html"]
    assert '<html lang="tr">' in turkish["html"]
    assert "other@example.com" not in turkish["body"]
    UserLanguagePreference.objects.update(language="en")
    UserLanguagePreference.objects.update_or_create(user=system.member, defaults={"language": "tr"})
    schedule.language = "tr" if first_language == "en" else "en"
    schedule.save(update_fields=["language"])
    Region.objects.filter(pk=system.region.pk).update(name="Renamed after preparation")
    newer = complete_saved(system, SLOT - timedelta(minutes=1))
    for delivery in deliveries:
        if delivery.pk == first.pk:
            continue
        assert process_notification(
            delivery.pk, SLOT + timedelta(seconds=1), sender=messages.append
        )
    assert process_notification(first.pk, first.available_at, sender=messages.append)
    current = list(NotificationDelivery.objects.all())
    assert {delivery.pk: delivery.prepared_message for delivery in current} == payloads
    assert {delivery.snapshot_id for delivery in current} == {snapshot.pk}
    assert snapshot.pk != newer.pk
    assert all(delivery.attempts == (2 if delivery.pk == first.pk else 1) for delivery in current)
    assert all(delivery.status == "sent" for delivery in current)
    assert messages[0].body == messages[-1].body
    assert len({message.extra_headers["Message-ID"] for message in messages}) == 3
    assert all(len(message.bcc) == 1 for message in messages)
    assert not process_notification(first.pk, SLOT + timedelta(minutes=5), sender=messages.append)
    assert len(messages) == 4
    assert RequestAttempt.objects.count() == requests


@pytest.mark.parametrize(
    ("language", "missing"),
    [
        ("en", "Tomorrow's forecast is unavailable."),
        ("tr", "Yar\u0131n\u0131n tahmini mevcut değil."),
    ],
)
def test_missing_saved_data_is_reported_in_the_pinned_language_without_inventing_zero(
    language: str, missing: str
) -> None:
    _system, schedule = configured()
    schedule.language = language
    schedule.save(update_fields=["language"])
    requests = RequestAttempt.objects.count()
    (delivery_id,) = enqueue_due(SLOT)
    messages: list[EmailMultiAlternatives] = []
    assert process_notification(delivery_id, SLOT, sender=messages.append)
    assert missing in messages[0].body
    assert "0.000 kWh/m²" not in messages[0].body
    assert "0,000 kWh/m²" not in messages[0].body
    sheet = attached_sheet(messages[0])
    for day in (10, 11):
        row = next(cell.row for cell in sheet["A"] if cell.value == datetime(2026, 11, day))
        assert sheet.cell(row, 3).value is None
    assert not any(isinstance(cell.value, int | float) for row in sheet for cell in row)
    assert RequestAttempt.objects.count() == requests


@pytest.mark.parametrize("first_language", ["en", "tr"])
def test_render_failure_freezes_distinct_plain_fallbacks_and_preserves_them_on_retry(
    first_language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    system, schedule = configured()
    schedule.language = first_language
    schedule.save(update_fields=["language"])
    complete_saved(system)
    enqueue_due(SLOT)
    pinned_deliveries(
        system,
        schedule,
        "tr" if first_language == "en" else "en",
        ["other@example.com", "external@example.com"],
    )
    first = NotificationDelivery.objects.get(schedule=schedule, recipients=[system.member.email])

    def broken(*args: object, **kwargs: object) -> EmailMultiAlternatives:
        raise RuntimeError("The normal renderer is unavailable.")

    monkeypatch.setattr("pyracast.notifications.preparation.build_message", broken)
    messages: list[EmailMultiAlternatives] = []

    def temporary_failure(message: EmailMultiAlternatives) -> None:
        messages.append(message)
        raise NotSentError

    assert not process_notification(first.pk, SLOT, sender=temporary_failure)
    first.refresh_from_db()
    deliveries = list(NotificationDelivery.objects.all())
    payloads = {delivery.pk: delivery.prepared_message for delivery in deliveries}
    for delivery in deliveries:
        assert delivery.prepared_message is not None
        assert delivery.prepared_message["format"] == "plain"
        assert delivery.snapshot_id is None
        body = delivery.prepared_message["body"]
        assert (
            "Yar\u0131n\u0131n kay\u0131tl\u0131 tahmini haz\u0131rlanamad\u0131."
            if delivery.language == "tr"
            else "Tomorrow's saved forecast could not be prepared."
        ) in body
    schedule.language = "en" if first_language == "tr" else "tr"
    schedule.save(update_fields=["language"])
    for delivery in deliveries:
        if delivery.pk == first.pk:
            continue
        assert process_notification(delivery.pk, delivery.available_at, sender=messages.append)
    assert process_notification(first.pk, first.available_at, sender=messages.append)
    assert {
        record.pk: record.prepared_message for record in NotificationDelivery.objects.all()
    } == payloads
    assert all(not message.attachments and not message.alternatives for message in messages)
    assert messages[0].body == messages[-1].body


@pytest.mark.parametrize("plain", [False, True])
def test_later_language_uses_the_already_frozen_slot_source_and_preparation_time(
    plain: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    system, schedule = configured()
    snapshot = complete_saved(system)
    (delivery_id,) = enqueue_due(SLOT)
    messages: list[EmailMultiAlternatives] = []
    if plain:

        def broken(*args: object, **kwargs: object) -> EmailMultiAlternatives:
            raise RuntimeError("The normal renderer is unavailable.")

        monkeypatch.setattr("pyracast.notifications.preparation.build_message", broken)
    assert process_notification(delivery_id, SLOT, sender=messages.append)
    original = NotificationDelivery.objects.get(pk=delivery_id)
    complete_saved(system, SLOT - timedelta(minutes=1))
    schedule.recipients = [*schedule.recipients, "other@example.com", "external@example.com"]
    schedule.language = "tr"
    schedule.save(update_fields=["recipients", "language"])
    assert enqueue_due(SLOT) == [delivery_id]
    later = pinned_deliveries(system, schedule, "tr", ["other@example.com", "external@example.com"])
    assert {address for record in later for address in record.recipients} == {
        "other@example.com",
        "external@example.com",
    }
    for record in later:
        assert process_notification(record.pk, SLOT + timedelta(minutes=1), sender=messages.append)
    latest = list(NotificationDelivery.objects.all())
    assert {record.snapshot_id for record in latest} == {None if plain else snapshot.pk}
    assert original.prepared_message is not None
    for record in latest:
        assert record.prepared_message is not None
        assert record.prepared_message["prepared_at"] == original.prepared_message["prepared_at"]
    assert messages[0].body != messages[1].body == messages[2].body
    assert (
        NotificationDelivery.objects.get(pk=delivery_id).prepared_message
        == original.prepared_message
    )


def test_later_languages_reuse_frozen_english_payload_when_turkish_is_processed_first() -> None:
    system, schedule = configured()
    snapshot = complete_saved(system)
    (english_id,) = enqueue_due(SLOT)
    messages: list[EmailMultiAlternatives] = []
    assert process_notification(english_id, SLOT, sender=messages.append)
    original = NotificationDelivery.objects.get(pk=english_id).prepared_message
    assert original is not None
    Region.objects.filter(pk=system.region.pk).update(name="Renamed after English was frozen")
    (english,) = pinned_deliveries(system, schedule, "en", ["late-english@example.com"])
    turkish, _ = pinned_deliveries(
        system, schedule, "tr", ["other@example.com", "external@example.com"]
    )
    assert process_notification(turkish.pk, SLOT + timedelta(minutes=1), sender=messages.append)
    english.refresh_from_db()
    assert english.prepared_message == original
    assert english.snapshot_id == snapshot.pk
    assert process_notification(english.pk, SLOT + timedelta(minutes=1), sender=messages.append)
    assert messages[2].body == messages[0].body
    assert messages[2].alternatives == messages[0].alternatives
    assert messages[2].attachments == messages[0].attachments
    assert NotificationDelivery.objects.get(pk=english_id).prepared_message == original


def test_later_language_render_failure_preserves_frozen_slot_provenance_and_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, schedule = configured()
    snapshot = complete_saved(system)
    (english_id,) = enqueue_due(SLOT)
    messages: list[EmailMultiAlternatives] = []
    assert process_notification(english_id, SLOT, sender=messages.append)
    english = NotificationDelivery.objects.get(pk=english_id)
    original = english.prepared_message
    assert original is not None
    Region.objects.filter(pk=system.region.pk).update(name="Renamed after English was frozen")
    newer = complete_saved(system, SLOT - timedelta(minutes=1))
    (late_english,) = pinned_deliveries(system, schedule, "en", ["late-english@example.com"])
    turkish, sibling = pinned_deliveries(
        system, schedule, "tr", ["other@example.com", "external@example.com"]
    )
    assert turkish.snapshot_id == newer.pk != snapshot.pk
    render = build_message

    def fail_turkish(
        record: NotificationDelivery,
        results: list[dict[str, object] | None] | None = None,
        *,
        prepared_at: datetime | None = None,
    ) -> EmailMultiAlternatives:
        if record.language == "tr":
            raise RuntimeError("The Turkish renderer is unavailable.")
        return render(record, results, prepared_at=prepared_at)

    monkeypatch.setattr("pyracast.notifications.preparation.build_message", fail_turkish)

    def temporary_failure(message: EmailMultiAlternatives) -> None:
        messages.append(message)
        raise NotSentError

    assert not process_notification(
        turkish.pk, SLOT + timedelta(minutes=1), sender=temporary_failure
    )
    turkish.refresh_from_db()
    fallback = turkish.prepared_message
    assert fallback is not None
    assert fallback["format"] == "plain"
    assert fallback["kind"] == "unavailable"
    assert turkish.snapshot_id == snapshot.pk
    assert fallback["prepared_at"] == original["prepared_at"]
    assert (
        "Yar\u0131n\u0131n kay\u0131tl\u0131 tahmini haz\u0131rlanamad\u0131." in fallback["body"]
    )
    assert "7,200" not in fallback["body"]
    sibling.refresh_from_db()
    assert sibling.snapshot_id == snapshot.pk
    assert sibling.prepared_message == fallback
    assert process_notification(sibling.pk, SLOT + timedelta(minutes=1), sender=messages.append)
    late_english.refresh_from_db()
    assert late_english.snapshot_id == snapshot.pk
    assert late_english.prepared_message == original
    assert process_notification(
        late_english.pk, SLOT + timedelta(minutes=1), sender=messages.append
    )
    assert messages[3].body == messages[0].body
    schedule.language = "en"
    schedule.save(update_fields=["language"])
    assert process_notification(turkish.pk, turkish.available_at, sender=messages.append)
    turkish.refresh_from_db()
    assert turkish.snapshot_id == snapshot.pk
    assert turkish.prepared_message == fallback
    assert messages[1].body == messages[2].body == messages[4].body
    assert all(not messages[index].attachments for index in (1, 2, 4))
    english.refresh_from_db()
    assert english.prepared_message == original
    assert english.snapshot_id == snapshot.pk
    assert english.attempts == 1


def test_mail_rendering_restores_the_caller_language_and_preserves_typed_workbook() -> None:
    system, _ = configured()
    complete_saved(system)
    (delivery_id,) = enqueue_due(SLOT)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    delivery.language = "tr"
    with translation.override("en"):
        message = build_message(delivery)
        assert translation.get_language() == "en"
    assert "Iş\u0131n\u0131m tahminidir" in message.body
    assert "3,299 kWh/m²" in message.body
    sheet = attached_sheet(message)
    assert sheet.title == "Tahmin"
    assert sheet["A5"].value == datetime(2026, 11, 10)
    assert sheet["A5"].number_format == "dd.mm.yyyy"
    assert isinstance(sheet["C5"].value, float)


def test_delivery_language_is_database_constrained() -> None:
    system, schedule = configured()
    with pytest.raises(IntegrityError), transaction.atomic():
        NotificationDelivery.objects.create(
            schedule=schedule,
            binding=system.binding,
            language="de",
            scheduled_for=SLOT,
            available_at=SLOT,
            recipients=["default@example.com"],
        )


@pytest.mark.parametrize("days", [1, 2])
def test_saved_forecast_updated_date_and_stale_warning_are_localized(days: int) -> None:
    system, _ = configured()
    snapshot = complete_saved(system, NOW - timedelta(days=days))
    (delivery_id,) = enqueue_due(SLOT)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    delivery.language = "tr"
    message = build_message(delivery)
    assert "Tahmin verisi 24 saatten eski." in message.body
    assert f"{9 - days} Kas 2026 16:00" in message.body
    assert attached_sheet(message)["A2"].value == (
        snapshot.collected_at.astimezone(ZoneInfo(system.region.timezone)).replace(tzinfo=None)
    )


def test_language_migration_preserves_prepared_content_and_does_not_resend() -> None:
    system, _ = configured()
    complete_saved(system)
    (delivery_id,) = enqueue_due(SLOT)
    messages: list[EmailMultiAlternatives] = []
    assert process_notification(delivery_id, SLOT, sender=messages.append)
    original = NotificationDelivery.objects.get(pk=delivery_id)
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("notifications", "0006_delivery_preparation")])
    finally:
        MigrationExecutor(connection).migrate(latest)
    restored = NotificationDelivery.objects.get(pk=delivery_id)
    assert restored.language == "en"
    assert restored.prepared_message == original.prepared_message
    assert restored.snapshot_id == original.snapshot_id
    assert restored.status == "sent"
    assert restored.attempts == 1
    assert restored.sent_at == original.sent_at
    assert not process_notification(
        delivery_id, SLOT + timedelta(minutes=1), sender=messages.append
    )
    assert len(messages) == 1
