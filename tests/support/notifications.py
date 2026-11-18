from datetime import datetime, timedelta
from io import BytesIO
from typing import Any
from zoneinfo import ZoneInfo

from django.core.mail import EmailMessage
from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from pyracast.calculation import calculate
from pyracast.connections.contracts import ProviderData
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
)
from pyracast.intervals import NormalizedInterval
from pyracast.notifications.models import (
    NotificationDelivery,
    ReminderSchedule,
)
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.scheduler import enqueue_due
from tests.support.domain import NOW, System, make_system

SLOT = NOW + timedelta(days=1) - timedelta(hours=6)


def attached_sheet(message: EmailMessage) -> Worksheet:
    assert len(message.attachments) == 1
    filename, content, mimetype = message.attachments[0]
    assert isinstance(filename, str)
    assert filename.endswith(".xlsx")
    assert mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert isinstance(content, bytes)
    sheet = load_workbook(BytesIO(content), data_only=True).active
    assert isinstance(sheet, Worksheet)
    return sheet


def configured() -> tuple[System, ReminderSchedule]:
    system = make_system(approved=True)
    system.member.email = "default@example.com"
    system.member.save()
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=[],
        times=["10:00"],
        now=NOW,
    )
    return system, schedule


def saved(
    system: System, when: datetime = NOW, *, intervals: list[dict[str, Any]] | None = None
) -> ForecastSnapshot:
    job = CollectionJob.objects.create(
        binding=system.binding,
        scheduled_for=when - timedelta(minutes=1),
        target_date=when.date() + timedelta(days=1),
        available_at=when,
        status="succeeded",
    )
    snapshot = ForecastSnapshot.objects.create(
        job=job,
        binding=system.binding,
        raw_payload={},
        normalized_intervals=[
            {
                "start": (SLOT + timedelta(days=offset)).isoformat(),
                "end": (SLOT + timedelta(days=offset, minutes=30)).isoformat(),
                "power_kw": 0.3,
                "sunlight": "day",
            }
            for offset in (0, 1)
        ]
        if intervals is None
        else intervals,
        collected_at=when,
        available_at=when,
        source_kind="forecast",
    )
    cache_saved_days(snapshot, when)
    return snapshot


def cache_saved_days(snapshot: ForecastSnapshot, when: datetime = NOW) -> None:
    data = ProviderData(
        snapshot.raw_payload,
        NormalizedInterval.from_rows(snapshot.normalized_intervals),
        snapshot.issued_at,
        snapshot.collected_at,
        source_kind="forecast",
    )
    days = (
        SLOT.astimezone(ZoneInfo(snapshot.binding.timezone)).date() + timedelta(days=offset)
        for offset in (0, 1)
    )
    payloads = {day.isoformat(): calculate(snapshot.binding, day, data) for day in days}
    ForecastResult.objects.update_or_create(
        snapshot=snapshot,
        defaults={"payload": {}, "daily_payloads": payloads, "published_at": when},
    )


def ready() -> NotificationDelivery:
    system, _ = configured()
    saved(system)
    ids = enqueue_due(SLOT)
    assert len(ids) == 1
    return NotificationDelivery.objects.get(pk=ids[0])
