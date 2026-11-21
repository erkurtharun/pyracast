import io
import json
from datetime import datetime, timedelta

import pytest
from django.core.management import CommandError, call_command

from pyracast.forecasts.models import CollectionJob, DispatcherPhase, DispatcherStatus
from pyracast.notifications.models import ReminderSchedule
from tests.support.delivery_health import NOW, answering_broker, recent_forecast
from tests.support.domain import make_system
from tests.support.notifications import store

# Operators and scripts read these keys of the printed report: keys may be added, but none of
# these is removed, renamed or retyped.
COUNTERS = (
    "invalid_reminder_recipients",
    "unvalidated_provider_credentials",
    "regions_without_recent_forecasts",
    "provider_cooldowns",
    "blocked_provider_credentials",
    "overdue_collections",
    "interrupted_collections",
    "failed_collections",
    "overdue_notifications",
    "interrupted_notifications",
    "failed_notifications",
    "missed_notifications",
    "unavailable_forecasts",
    "partial_forecasts",
    "mail_authentication_failures",
    "overdue_outbound_mail",
    "failed_outbound_mail",
    "uncertain_outbound_mail",
    "overdue_metering",
    "interrupted_metering",
    "failed_metering",
    "saved_forecast_notifications",
    "paused_notifications",
)


def printed_report(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, object], bool]:
    monkeypatch.setattr(
        "pyracast.orchestration.management.commands.check_delivery_health.timezone.now", lambda: NOW
    )
    answering_broker(monkeypatch)
    output = io.StringIO()
    try:
        call_command("check_delivery_health", stdout=output)
    except CommandError:
        failed = True
    else:
        failed = False
    printed = output.getvalue()
    assert printed.endswith("\n")
    assert printed.count("\n") == 1
    report = json.loads(printed)
    assert list(report) == sorted(report)
    return report, failed


def assert_contract(report: dict[str, object], failed: bool) -> None:
    assert type(report["healthy"]) is bool
    assert report["healthy"] is not failed
    assert isinstance(report["checked_at"], str)
    assert datetime.fromisoformat(report["checked_at"]) == NOW
    phases = report["unhealthy_dispatch_phases"]
    assert isinstance(phases, list)
    assert set(phases) <= set(DispatcherPhase.values)
    assert all(type(phase) is str for phase in phases)
    for key in COUNTERS:
        value = report[key]
        assert type(value) is int, key
        assert value >= 0, key


@pytest.mark.django_db(transaction=True)
def test_the_printed_report_keeps_every_key_with_its_type_on_an_empty_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, failed = printed_report(monkeypatch)

    assert_contract(report, failed)
    assert failed is True
    assert report["unhealthy_dispatch_phases"] == list(DispatcherPhase.values)
    assert all(report[key] == 0 for key in COUNTERS)


@pytest.mark.django_db(transaction=True)
def test_the_printed_report_keeps_every_key_with_its_type_on_degraded_and_healthy_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    recent_forecast(system)
    for phase in DispatcherPhase.values:
        DispatcherStatus.objects.update_or_create(
            phase=phase, defaults={"attempted_at": NOW, "succeeded_at": NOW, "last_error": ""}
        )
    report, failed = printed_report(monkeypatch)
    assert_contract(report, failed)
    assert failed is False
    assert report["unhealthy_dispatch_phases"] == []

    store(
        ReminderSchedule.objects.create(region=system.region, enabled=True, effective_from=NOW),
        ["not-an-address"],
    )
    CollectionJob.objects.create(
        binding=system.binding,
        target_date=NOW.date(),
        scheduled_for=NOW - timedelta(hours=3),
        available_at=NOW - timedelta(hours=3),
    )
    system.account.request_blocked_until = NOW + timedelta(minutes=1)
    system.account.save(update_fields=("request_blocked_until",))
    report, failed = printed_report(monkeypatch)

    assert_contract(report, failed)
    assert failed is True
    assert report["invalid_reminder_recipients"] == 1
    assert report["overdue_collections"] == 1
    assert report["provider_cooldowns"] == 1
