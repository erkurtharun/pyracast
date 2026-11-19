from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.utils import translation

from pyracast.accounts.models import UserLanguagePreference
from pyracast.forecasts.models import RequestAttempt
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.mail import build_message
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.scheduler import enqueue_due
from tests.support.domain import NOW
from tests.support.notification_preparation import complete_saved
from tests.support.notifications import SLOT, attached_sheet, configured

pytestmark = pytest.mark.django_db(transaction=True)


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
    with (
        pytest.raises(IntegrityError, match="notificationdelivery_language_chk"),
        transaction.atomic(),
    ):
        NotificationDelivery.objects.create(
            schedule=schedule,
            binding=system.binding,
            language="de",
            scheduled_for=SLOT,
            preparation_deadline=SLOT,
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
