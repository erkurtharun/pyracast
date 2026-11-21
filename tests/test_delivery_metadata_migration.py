from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from tests.support.domain import NOW, make_system
from tests.support.migrations import forward, migrate

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = ("notifications", "0009_purge_warning_mail")
WORKBOOK = {"format": "xlsx", "subject": "Forecast", "html": "<p>Forecast</p>"}
PLAIN = {"format": "plain", "subject": "Unavailable", "body": "Not prepared."}
UNREADABLE = {**PLAIN, "kind": "unavailable", "prepared_at": "yesterday"}
NAIVE = {**PLAIN, "kind": "current", "prepared_at": "2026-11-09T08:00:00"}
UNNAMED = {**PLAIN, "prepared_at": "2026-11-09T08:00:00+00:00"}


def test_preparation_metadata_moves_from_the_message_into_typed_columns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    system = make_system()
    schedule = ReminderSchedule.objects.create(region=system.region, effective_from=NOW)
    migrate(PREVIOUS)
    try:
        apps = MigrationExecutor(connection).loader.project_state([PREVIOUS]).apps
        deliveries = apps.get_model("notifications", "NotificationDelivery")._default_manager

        def stored(hours: int, message: dict[str, object] | None) -> int:
            return int(
                deliveries.create(
                    schedule_id=schedule.pk,
                    binding_id=system.binding.pk,
                    scheduled_for=NOW - timedelta(hours=hours),
                    preparation_deadline=NOW,
                    available_at=NOW,
                    status="sent",
                    prepared_message=message,
                ).pk
            )

        workbook = stored(
            1,
            {
                **WORKBOOK,
                "kind": "saved_partial",
                "tomorrow_complete": False,
                "prepared_at": "2026-11-09T10:00:00+00:00",
            },
        )
        plain = stored(
            2, {**PLAIN, "kind": "unavailable", "prepared_at": "2026-11-09T09:00:00+03:00"}
        )
        skipped = [stored(3, UNREADABLE), stored(4, NAIVE), stored(5, UNNAMED)]
        unprepared = stored(6, None)
        capsys.readouterr()
    finally:
        forward()

    rows = NotificationDelivery.objects.in_bulk()
    columns = {
        pk: (row.forecast_kind, row.forecast_complete, row.prepared_at, row.prepared_message)
        for pk, row in rows.items()
    }
    assert columns[workbook] == (
        "saved_partial",
        False,
        datetime(2026, 11, 9, 10, tzinfo=UTC),
        WORKBOOK,
    )
    assert columns[plain] == ("unavailable", None, datetime(2026, 11, 9, 6, tzinfo=UTC), PLAIN)
    assert [columns[pk] for pk in skipped] == [
        ("", None, None, UNREADABLE),
        ("", None, None, NAIVE),
        ("", None, None, UNNAMED),
    ]
    assert columns[unprepared] == ("", None, None, None)
    assert (
        "notifications: 3 prepared delivery message(s) carried no readable kind and time "
        "and kept their message unchanged." in capsys.readouterr().out
    )

    migrate(PREVIOUS)
    try:
        apps = MigrationExecutor(connection).loader.project_state([PREVIOUS]).apps
        restored = dict(
            apps.get_model("notifications", "NotificationDelivery")
            ._default_manager.filter(pk__in=(workbook, plain))
            .values_list("pk", "prepared_message")
        )
    finally:
        forward()

    assert restored == {
        workbook: {
            **WORKBOOK,
            "kind": "saved_partial",
            "tomorrow_complete": False,
            "prepared_at": "2026-11-09T10:00:00+00:00",
        },
        plain: {**PLAIN, "kind": "unavailable", "prepared_at": "2026-11-09T06:00:00+00:00"},
    }
