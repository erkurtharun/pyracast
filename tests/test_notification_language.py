from datetime import datetime, timedelta

import pytest
from django.contrib.auth.models import User
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import translation

from pyracast.accounts.models import Membership, UserLanguagePreference
from pyracast.forecasts.models import RequestAttempt
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.mail import NotSentError, build_message
from pyracast.notifications.models import NotificationDelivery, ReminderPreference
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.scheduler import enqueue_due
from pyracast.regions.models import Region
from tests.test_domain import NOW, System
from tests.test_notification_preparation import complete_saved
from tests.test_notifications import SLOT, configured

pytestmark = pytest.mark.django_db(transaction=True)


def other_owner(system: System, language: str) -> ReminderPreference:
    member = User.objects.create(username="other-reminder", email="other@example.com")
    Membership.objects.create(user=member, tenant=system.tenant)
    UserLanguagePreference.objects.create(user=member, language=language)
    return configure_reminders(
        member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["other@example.com", "external@example.com"],
        times=["10:00"],
        now=NOW,
    )


def late_english_owner(system: System) -> ReminderPreference:
    member = User.objects.create(username="late-english", email="late-english@example.com")
    Membership.objects.create(user=member, tenant=system.tenant)
    return configure_reminders(
        member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=[],
        times=["10:00"],
        now=NOW,
    )


@pytest.mark.parametrize("language", [None, "en", "tr"])
def test_schedule_pins_owner_language_for_every_recipient_and_defaults_to_english(
    language: str | None,
) -> None:
    system, preference = configured()
    recipient_user = User.objects.create(username="recipient", email="recipient@example.com")
    UserLanguagePreference.objects.create(user=recipient_user, language="tr")
    preference.recipients = [recipient_user.email, "external@example.com"]
    preference.save()
    if language is not None:
        UserLanguagePreference.objects.create(user=system.member, language=language)
    with translation.override("tr" if language != "tr" else "en"):
        ids = enqueue_due(SLOT)
    assert len(ids) == 2
    assert set(NotificationDelivery.objects.values_list("language", flat=True)) == {
        language or "en"
    }
    UserLanguagePreference.objects.update_or_create(
        user=system.member, defaults={"language": "tr" if language != "tr" else "en"}
    )
    assert enqueue_due(SLOT) == ids
    assert set(NotificationDelivery.objects.values_list("language", flat=True)) == {
        language or "en"
    }


@pytest.mark.parametrize("first_language", ["en", "tr"])
def test_mixed_languages_share_one_frozen_source_and_retries_keep_original_content(
    first_language: str,
) -> None:
    system, _ = configured()
    snapshot = complete_saved(system)
    other_owner(system, "tr")
    requests = RequestAttempt.objects.count()
    enqueue_due(SLOT)
    first = NotificationDelivery.objects.filter(language=first_language).order_by("pk").first()
    assert first is not None
    messages: list[EmailMultiAlternatives] = []

    def temporary_failure(message: EmailMultiAlternatives) -> None:
        messages.append(message)
        raise NotSentError

    assert not process_notification(first.pk, SLOT, sender=temporary_failure)
    first.refresh_from_db()
    deliveries = list(NotificationDelivery.objects.order_by("pk"))
    assert len(deliveries) == 3
    assert {delivery.snapshot_id for delivery in deliveries} == {snapshot.pk}
    payloads = [delivery.prepared_message for delivery in deliveries]
    assert all(isinstance(payload, dict) for payload in payloads)
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
    assert english["csv"] == turkish["csv"]
    assert "Daily energy: 7.200 kWh/m²" in english["body"]
    assert "Günlük enerji: 7,200 kWh/m²" in turkish["body"]
    assert '<html lang="en">' in english["html"]
    assert '<html lang="tr">' in turkish["html"]
    assert "other@example.com" not in turkish["body"]
    UserLanguagePreference.objects.update(language="en")
    UserLanguagePreference.objects.update_or_create(user=system.member, defaults={"language": "tr"})
    Region.objects.filter(pk=system.region.pk).update(name="Renamed after preparation")
    newer = complete_saved(system, SLOT - timedelta(minutes=1))
    for delivery in deliveries:
        if delivery.pk == first.pk:
            continue
        assert process_notification(
            delivery.pk, SLOT + timedelta(seconds=1), sender=messages.append
        )
    assert process_notification(first.pk, first.available_at, sender=messages.append)
    current = list(NotificationDelivery.objects.order_by("pk"))
    assert [delivery.prepared_message for delivery in current] == payloads
    assert {delivery.snapshot_id for delivery in current} == {snapshot.pk}
    assert snapshot.pk != newer.pk
    assert all(delivery.attempts == (2 if delivery.pk == first.pk else 1) for delivery in current)
    assert all(delivery.status == "sent" for delivery in current)
    assert messages[0].body == messages[-1].body
    assert len({message.extra_headers["Message-ID"] for message in messages}) == 3
    assert all(len(message.to) == 1 for message in messages)
    assert not process_notification(
        current[0].pk, SLOT + timedelta(minutes=5), sender=messages.append
    )
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
    system, _ = configured()
    UserLanguagePreference.objects.create(user=system.member, language=language)
    requests = RequestAttempt.objects.count()
    delivery_id = enqueue_due(SLOT)[0]
    messages: list[EmailMultiAlternatives] = []
    assert process_notification(delivery_id, SLOT, sender=messages.append)
    assert missing in messages[0].body
    assert "0.000 kWh/m²" not in messages[0].body
    assert "0,000 kWh/m²" not in messages[0].body
    assert len(str(messages[0].attachments[0][1]).splitlines()) == 1
    assert RequestAttempt.objects.count() == requests


@pytest.mark.parametrize("first_language", ["en", "tr"])
def test_render_failure_freezes_distinct_plain_fallbacks_and_preserves_them_on_retry(
    first_language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    system, _ = configured()
    UserLanguagePreference.objects.create(user=system.member, language=first_language)
    other_owner(system, "tr" if first_language == "en" else "en")
    complete_saved(system)
    ids = enqueue_due(SLOT)

    def broken(*args: object, **kwargs: object) -> EmailMultiAlternatives:
        raise RuntimeError("The normal renderer is unavailable.")

    monkeypatch.setattr("pyracast.notifications.preparation.build_message", broken)
    messages: list[EmailMultiAlternatives] = []

    def temporary_failure(message: EmailMultiAlternatives) -> None:
        messages.append(message)
        raise NotSentError

    assert not process_notification(ids[0], SLOT, sender=temporary_failure)
    deliveries = list(NotificationDelivery.objects.order_by("pk"))
    payloads = [delivery.prepared_message for delivery in deliveries]
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
    UserLanguagePreference.objects.update(language="en" if first_language == "tr" else "tr")
    for delivery in deliveries:
        assert process_notification(delivery.pk, delivery.available_at, sender=messages.append)
    assert [record.prepared_message for record in NotificationDelivery.objects.order_by("pk")] == (
        payloads
    )
    assert all(not message.attachments and not message.alternatives for message in messages)
    assert messages[0].body == messages[1].body


@pytest.mark.parametrize("plain", [False, True])
def test_later_language_uses_the_already_frozen_slot_source_and_preparation_time(
    plain: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    system, _ = configured()
    snapshot = complete_saved(system)
    delivery_id = enqueue_due(SLOT)[0]
    messages: list[EmailMultiAlternatives] = []
    if plain:

        def broken(*args: object, **kwargs: object) -> EmailMultiAlternatives:
            raise RuntimeError("The normal renderer is unavailable.")

        monkeypatch.setattr("pyracast.notifications.preparation.build_message", broken)
    assert process_notification(delivery_id, SLOT, sender=messages.append)
    original = NotificationDelivery.objects.get(pk=delivery_id)
    other_owner(system, "tr")
    complete_saved(system, SLOT - timedelta(minutes=1))
    ids = enqueue_due(SLOT)
    for pk in ids[1:]:
        assert process_notification(pk, SLOT + timedelta(minutes=1), sender=messages.append)
    latest = list(NotificationDelivery.objects.order_by("pk"))
    assert {record.snapshot_id for record in latest} == {None if plain else snapshot.pk}
    assert original.prepared_message is not None
    for record in latest:
        assert record.prepared_message is not None
        assert record.prepared_message["prepared_at"] == original.prepared_message["prepared_at"]
    assert messages[0].body != messages[1].body == messages[2].body
    original.refresh_from_db()
    assert original.prepared_message == latest[0].prepared_message


def test_later_languages_reuse_frozen_english_payload_when_turkish_is_processed_first() -> None:
    system, _ = configured()
    snapshot = complete_saved(system)
    english_id = enqueue_due(SLOT)[0]
    messages: list[EmailMultiAlternatives] = []
    assert process_notification(english_id, SLOT, sender=messages.append)
    original = NotificationDelivery.objects.get(pk=english_id).prepared_message
    assert original is not None
    Region.objects.filter(pk=system.region.pk).update(name="Renamed after English was frozen")
    english_owner = late_english_owner(system)
    turkish_owner = other_owner(system, "tr")
    enqueue_due(SLOT)
    turkish = NotificationDelivery.objects.filter(preference=turkish_owner).order_by("pk").first()
    assert turkish is not None
    assert process_notification(turkish.pk, SLOT + timedelta(minutes=1), sender=messages.append)
    english = NotificationDelivery.objects.get(preference=english_owner)
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
    system, _ = configured()
    snapshot = complete_saved(system)
    english_id = enqueue_due(SLOT)[0]
    messages: list[EmailMultiAlternatives] = []
    assert process_notification(english_id, SLOT, sender=messages.append)
    english = NotificationDelivery.objects.get(pk=english_id)
    original = english.prepared_message
    assert original is not None
    Region.objects.filter(pk=system.region.pk).update(name="Renamed after English was frozen")
    english_owner = late_english_owner(system)
    preference = other_owner(system, "tr")
    newer = complete_saved(system, SLOT - timedelta(minutes=1))
    enqueue_due(SLOT)
    turkish, sibling = list(
        NotificationDelivery.objects.filter(preference=preference).order_by("pk")
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
    late_english = NotificationDelivery.objects.get(preference=english_owner)
    assert late_english.snapshot_id == snapshot.pk
    assert late_english.prepared_message == original
    assert process_notification(
        late_english.pk, SLOT + timedelta(minutes=1), sender=messages.append
    )
    assert messages[3].body == messages[0].body
    UserLanguagePreference.objects.filter(user=preference.user).update(language="en")
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


def test_mail_rendering_restores_the_caller_language_and_preserves_technical_csv() -> None:
    system, _ = configured()
    complete_saved(system)
    delivery = NotificationDelivery.objects.get(pk=enqueue_due(SLOT)[0])
    delivery.language = "tr"
    with translation.override("en"):
        message = build_message(delivery)
        assert translation.get_language() == "en"
    assert "doğrulanmam\u0131ş" in message.body
    assert "7,200 kWh/m²" in message.body
    assert "local_date,start,end,ghi_wm2,quality_flags" in str(message.attachments[0][1])
    assert "unvalidated" in str(message.attachments[0][1])


def test_delivery_language_is_database_constrained() -> None:
    system, preference = configured()
    with pytest.raises(IntegrityError), transaction.atomic():
        NotificationDelivery.objects.create(
            preference=preference,
            binding=system.binding,
            language="de",
            scheduled_for=SLOT,
            available_at=SLOT,
            recipient="default@example.com",
        )


@pytest.mark.parametrize("days", [1, 2])
def test_saved_forecast_age_uses_translated_day_units(days: int) -> None:
    system, _ = configured()
    complete_saved(system, NOW - timedelta(days=days))
    delivery = NotificationDelivery.objects.get(pk=enqueue_due(SLOT)[0])
    delivery.language = "tr"
    assert f"{days} gün, 18:00:00" in build_message(delivery).body


def test_language_migration_preserves_prepared_content_and_does_not_resend() -> None:
    system, _ = configured()
    complete_saved(system)
    delivery_id = enqueue_due(SLOT)[0]
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
