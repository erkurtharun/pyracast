import io
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from dramatiq.brokers.rabbitmq import RabbitmqBroker
from dramatiq.common import xq_name

from pyracast.accounts.models import (
    EVENT_MAIL,
    Membership,
    OutboundMail,
    OutboundMailError,
    OutboundMailStatus,
    Tenant,
    WorkspaceRole,
)
from pyracast.background.failures import ItemsFailedError
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    DispatcherPhase,
    DispatcherStatus,
    RequestAttempt,
    RequestPurpose,
)
from pyracast.notifications.models import ReminderSchedule
from pyracast.orchestration import dispatcher
from pyracast.orchestration.kinds import QUEUES
from pyracast.plants.calibration_runs import requeue_run, start_run
from pyracast.plants.models import CalibrationError, CalibrationRun, Plant, PlantConfiguration
from pyracast.plants.training import LEASE_DURATION as LEASE
from pyracast.regions.models import Region
from tests.support.budgets import assert_scale_invariant, captured_statements
from tests.support.delivery_health import NOW, AnsweringBroker, recent_forecast
from tests.support.domain import System, make_system
from tests.support.notifications import store
from tests.support.plant_operations import make_plant
from tests.support.rabbitmq import install_fake_rabbitmq

pytestmark = pytest.mark.django_db(transaction=True)
LAG = timedelta(minutes=5)
BEFORE = timedelta(microseconds=1)
COMMAND = "pyracast.orchestration.management.commands.check_delivery_health"


def unreachable_broker() -> RabbitmqBroker:
    raise ImproperlyConfigured("no broker in this test")


def printed(
    monkeypatch: pytest.MonkeyPatch,
    *arguments: str,
    broker: Callable[[], RabbitmqBroker] = lambda: cast(RabbitmqBroker, AnsweringBroker()),
) -> tuple[dict[str, Any], bool]:
    monkeypatch.setattr(f"{COMMAND}.timezone.now", lambda: NOW)
    monkeypatch.setattr("pyracast.background.broker.make_broker", broker)
    output = io.StringIO()
    try:
        call_command("check_delivery_health", *arguments, stdout=output)
    except CommandError:
        failed = True
    else:
        failed = False
    report: dict[str, Any] = json.loads(output.getvalue())
    assert report["healthy"] is not failed
    return report, failed


def dispatched(at: datetime = NOW) -> None:
    for phase in DispatcherPhase.values:
        DispatcherStatus.objects.update_or_create(
            phase=phase, defaults={"attempted_at": at, "succeeded_at": at, "last_error": ""}
        )


def healthy_plant(monkeypatch: pytest.MonkeyPatch) -> tuple[System, Plant]:
    system, plant = make_plant(monkeypatch)
    recent_forecast(system)
    dispatched()
    return system, plant


def calibration(system: System, plant: Plant, **fields: Any) -> CalibrationRun:
    run = CalibrationRun.objects.create(
        plant=plant,
        configuration=PlantConfiguration.objects.get(plant=plant),
        binding=system.binding,
        requested_by=system.member,
        window_start=NOW.date() - timedelta(days=7),
        window_end=NOW.date() - timedelta(days=1),
        cutoff_at=NOW - timedelta(days=1),
        **fields,
    )
    return run


CALIBRATION_KEYS = (
    "queued_calibrations",
    "running_calibrations",
    "overdue_calibrations",
    "interrupted_calibrations",
    "failed_calibrations",
)


def calibration_counts(report: dict[str, Any]) -> tuple[int, ...]:
    return tuple(report[key] for key in CALIBRATION_KEYS)


def test_a_waiting_calibration_is_queued_until_it_waits_longer_than_the_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = healthy_plant(monkeypatch)
    run = calibration(system, plant)
    CalibrationRun.objects.filter(pk=run.pk).update(created_at=NOW - LAG)
    report, failed = printed(monkeypatch)
    assert calibration_counts(report) == (1, 0, 0, 0, 0)
    assert failed is False

    CalibrationRun.objects.filter(pk=run.pk).update(created_at=NOW - LAG - BEFORE)
    report, failed = printed(monkeypatch)
    assert calibration_counts(report) == (1, 0, 1, 0, 0)
    assert failed is True

    CalibrationRun.objects.filter(pk=run.pk).update(attempts=1)
    report, failed = printed(monkeypatch)
    assert calibration_counts(report) == (1, 0, 0, 0, 0)
    assert failed is False


@pytest.mark.parametrize(
    ("created_at", "cutoff_at", "overdue"),
    [
        pytest.param(NOW - LAG - LEASE, NOW - LAG, 0, id="cutoff-ahead-a-lag-ago"),
        pytest.param(NOW - LAG - LEASE, NOW - LAG - BEFORE, 1, id="cutoff-ahead-past-the-lag"),
        pytest.param(NOW - BEFORE, NOW + LEASE, 0, id="cutoff-ahead-of-the-check"),
        pytest.param(NOW - LAG - LEASE, NOW + timedelta(days=1), 0, id="far-cutoff-a-lease"),
        pytest.param(
            NOW - LAG - LEASE - BEFORE, NOW + timedelta(days=1), 1, id="far-cutoff-past-a-lease"
        ),
        pytest.param(NOW - LAG, NOW - timedelta(days=1), 0, id="cutoff-behind-a-lag-ago"),
        pytest.param(NOW - LAG - BEFORE, NOW - timedelta(days=1), 1, id="cutoff-behind-past"),
        pytest.param(NOW + timedelta(hours=1), NOW + timedelta(hours=1), 0, id="created-ahead"),
    ],
)
def test_a_calibration_waiting_for_its_cutoff_is_overdue_a_lag_after_the_claim_may_take_it(
    monkeypatch: pytest.MonkeyPatch, created_at: datetime, cutoff_at: datetime, overdue: int
) -> None:
    system, plant = healthy_plant(monkeypatch)
    run = calibration(system, plant)
    CalibrationRun.objects.filter(pk=run.pk).update(created_at=created_at, cutoff_at=cutoff_at)

    report, failed = printed(monkeypatch)

    assert calibration_counts(report) == (1, 0, overdue, 0, 0)
    assert failed is bool(overdue)


def test_a_requeued_calibration_stays_queued_and_is_never_overdue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = healthy_plant(monkeypatch)
    run = calibration(system, plant)
    started = NOW - timedelta(days=1)
    start_run(run, started, LEASE)
    requeue_run(run, CalibrationError.INVALID_CALIBRATION_DATA)
    CalibrationRun.objects.filter(pk=run.pk).update(created_at=started)

    report, failed = printed(monkeypatch)

    assert CalibrationRun.objects.filter(pk=run.pk, status="pending", attempts=1).exists()
    assert calibration_counts(report) == (1, 0, 0, 0, 0)
    assert failed is False


def test_a_running_calibration_is_interrupted_once_its_lease_has_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = healthy_plant(monkeypatch)
    run = calibration(
        system,
        plant,
        status="running",
        attempts=1,
        lease_token=uuid.uuid4(),
        lease_expires_at=NOW + BEFORE,
    )
    report, failed = printed(monkeypatch)
    assert calibration_counts(report) == (0, 1, 0, 0, 0)
    assert failed is False

    CalibrationRun.objects.filter(pk=run.pk).update(lease_expires_at=NOW)
    report, failed = printed(monkeypatch)
    assert calibration_counts(report) == (0, 0, 0, 1, 0)
    assert failed is True

    Region.objects.filter(pk=system.region.pk).update(deleted_at=NOW)
    report, _ = printed(monkeypatch)
    assert calibration_counts(report) == (0, 0, 0, 0, 0)


def test_a_failed_calibration_counts_for_one_day_after_it_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = healthy_plant(monkeypatch)
    run = calibration(
        system,
        plant,
        status="failed",
        attempts=3,
        last_error="attempt_limit",
        completed_at=NOW - timedelta(days=1),
    )
    report, failed = printed(monkeypatch)
    assert calibration_counts(report) == (0, 0, 0, 0, 1)
    assert failed is True

    CalibrationRun.objects.filter(pk=run.pk).update(completed_at=NOW - timedelta(days=1) - BEFORE)
    report, failed = printed(monkeypatch)
    assert calibration_counts(report) == (0, 0, 0, 0, 0)
    assert failed is False


def attempts(system: System, binding: SiteBinding | None, days_ago: int, count: int) -> None:
    for _ in range(count):
        RequestAttempt.objects.create(
            provider_account=system.account,
            binding=binding,
            utc_day=NOW.date() - timedelta(days=days_ago),
            created_at=NOW - timedelta(days=days_ago),
            purpose=RequestPurpose.FORECAST,
        )


def test_provider_requests_are_reported_per_binding_and_utc_day_over_seven_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    recent_forecast(system)
    dispatched()
    other = SiteBinding.objects.create(
        region=Region.objects.create(
            tenant=system.tenant,
            name="Other",
            latitude=41,
            longitude=29,
            timezone="Europe/Istanbul",
        ),
        provider_account=system.account,
        region_revision=1,
        site_id="other",
        capacity_kw=1,
        latitude=41,
        longitude=29,
        timezone="Europe/Istanbul",
    )
    attempts(system, system.binding, 0, 3)
    attempts(system, system.binding, 6, 5)
    attempts(system, system.binding, 7, 9)
    attempts(system, other, 0, 4)
    attempts(system, None, 0, 8)

    report, failed = printed(monkeypatch)

    assert report["provider_requests_max_binding_today"] == 4
    assert report["provider_requests_max_binding_day"] == 5
    assert failed is False


class Broker:
    def __init__(self, channel: Channel | None) -> None:
        self._channel = channel
        self.closed = 0

    @property
    def channel(self) -> Channel:
        if self._channel is None:
            raise ConnectionError("the broker is down")
        return self._channel

    def close(self) -> None:
        self.closed += 1


class Channel:
    def __init__(self, counts: dict[str, tuple[int, int]]) -> None:
        self.counts = counts
        self.declared: list[tuple[str, bool]] = []

    def queue_declare(self, queue: str, passive: bool = False) -> SimpleNamespace:
        self.declared.append((queue, passive))
        messages, consumers = self.counts[queue]
        return SimpleNamespace(
            method=SimpleNamespace(message_count=messages, consumer_count=consumers)
        )


def test_the_broker_section_reports_each_queue_and_its_counts_never_decide_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    recent_forecast(system)
    dispatched()
    channel = Channel(
        {
            "default": (3, 1),
            "default.XQ": (0, 0),
            "calibration": (0, 0),
            "calibration.XQ": (2, 0),
            "notifications": (1, 1),
            "notifications.XQ": (0, 0),
            "epias": (0, 1),
            "epias.XQ": (5, 0),
        }
    )
    broker = Broker(channel)

    report, failed = printed(monkeypatch, broker=lambda: cast(RabbitmqBroker, broker))

    assert failed is False
    declared = {"missing": False, "dead_letters_missing": False}
    assert report["broker"] == {
        "reachable": True,
        "queues": {
            "default": {"messages": 3, "consumers": 1, "dead_letters": 0, **declared},
            "calibration": {"messages": 0, "consumers": 0, "dead_letters": 2, **declared},
            "notifications": {"messages": 1, "consumers": 1, "dead_letters": 0, **declared},
            "epias": {"messages": 0, "consumers": 1, "dead_letters": 5, **declared},
        },
    }
    assert all(passive for _, passive in channel.declared)
    assert broker.closed == 1


@pytest.mark.parametrize("failure", ["configuration", "connection"])
def test_a_broker_that_does_not_answer_fails_the_report_while_every_phase_succeeded(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    system = make_system(approved=True)
    recent_forecast(system)
    dispatched()
    broker = Broker(None)

    report, failed = printed(
        monkeypatch,
        broker=unreachable_broker
        if failure == "configuration"
        else lambda: cast(RabbitmqBroker, broker),
    )

    assert failed is True
    assert report["broker"] == {"reachable": False, "queues": {}}
    assert report["unhealthy_dispatch_phases"] == []
    assert broker.closed == (failure == "connection")


def test_the_broker_section_reads_a_lost_connection_again_on_a_fresh_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    recent_forecast(system)
    dispatched()
    rabbitmq = install_fake_rabbitmq(monkeypatch)
    rabbitmq.declare(*QUEUES, *(xq_name(queue) for queue in QUEUES))
    broker = RabbitmqBroker(url="amqp://broker.invalid/", confirm_delivery=True)
    assert broker.channel.is_open
    rabbitmq.drop()

    report, failed = printed(monkeypatch, broker=lambda: broker)

    assert failed is False
    assert report["broker"]["reachable"] is True
    assert rabbitmq.attempts == 2
    rabbitmq.reachable = False

    report, failed = printed(
        monkeypatch,
        broker=lambda: RabbitmqBroker(url="amqp://broker.invalid/", confirm_delivery=True),
    )

    assert failed is True
    assert report["broker"] == {"reachable": False, "queues": {}}
    assert rabbitmq.attempts == 4


def fake_broker_report(
    monkeypatch: pytest.MonkeyPatch, *declared: str, refusal: int | None = None
) -> tuple[dict[str, Any], bool, int]:
    system = make_system(approved=True)
    recent_forecast(system)
    dispatched()
    rabbitmq = install_fake_rabbitmq(monkeypatch)
    rabbitmq.declare(*declared)
    rabbitmq.refusal = refusal
    rabbitmq.queues["default"].extend([b"waiting"] * 2)
    report, failed = printed(
        monkeypatch,
        broker=lambda: RabbitmqBroker(url="amqp://broker.invalid/", confirm_delivery=True),
    )
    return report, failed, rabbitmq.attempts


def test_a_work_queue_the_broker_has_not_declared_fails_the_report_and_keeps_the_other_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declared = [queue for queue in QUEUES if queue != "notifications"]

    report, failed, connections = fake_broker_report(
        monkeypatch, *declared, *(xq_name(queue) for queue in declared)
    )

    assert failed is True
    assert report["unhealthy_dispatch_phases"] == []
    assert report["broker"]["reachable"] is True
    assert report["broker"]["queues"]["notifications"] == {
        "messages": None,
        "consumers": None,
        "dead_letters": None,
        "missing": True,
        "dead_letters_missing": True,
    }
    assert report["broker"]["queues"]["default"] == {
        "messages": 2,
        "consumers": 0,
        "dead_letters": 0,
        "missing": False,
        "dead_letters_missing": False,
    }
    assert connections == 1


def test_a_missing_dead_letter_queue_of_a_declared_queue_leaves_the_report_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, failed, connections = fake_broker_report(
        monkeypatch, *QUEUES, *(xq_name(queue) for queue in QUEUES if queue != "epias")
    )

    assert failed is False
    assert report["broker"]["reachable"] is True
    assert report["broker"]["queues"]["epias"] == {
        "messages": 0,
        "consumers": 0,
        "dead_letters": None,
        "missing": False,
        "dead_letters_missing": True,
    }
    assert report["broker"]["queues"]["default"]["messages"] == 2
    assert connections == 1


def test_a_queue_read_the_broker_refuses_for_another_reason_is_a_broker_that_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, failed, connections = fake_broker_report(monkeypatch, *QUEUES, refusal=403)

    assert failed is True
    assert report["broker"] == {"reachable": False, "queues": {}}
    assert connections == 2


def test_a_new_plant_waits_for_its_open_first_collection_before_it_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    dispatched()
    first = CollectionJob.objects.get(binding=system.binding)
    CollectionJob.objects.filter(pk=first.pk).update(available_at=NOW)

    report, failed = printed(monkeypatch)
    assert report["regions_without_recent_forecasts"] == 0
    assert failed is False

    CollectionJob.objects.filter(pk=first.pk).update(status="failed", scheduled_for=NOW)
    report, failed = printed(monkeypatch)
    assert report["regions_without_recent_forecasts"] == 1
    assert report["failed_collections"] == 1
    assert failed is True


MAIL_OUTCOMES = ("overdue_outbound_mail", "failed_outbound_mail", "uncertain_outbound_mail")


def ended_event_mail(status: OutboundMailStatus, error: OutboundMailError) -> None:
    for kind in EVENT_MAIL:
        OutboundMail.objects.create(
            kind=kind,
            recipient="administrator@example.com",
            language="en",
            context={"subject_id": "1"},
            status=status,
            last_error=error,
            created_at=NOW - timedelta(hours=1),
            available_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(days=1),
            finished_at=NOW - timedelta(minutes=30),
        )


@pytest.mark.parametrize(
    "error", [OutboundMailError.SUBJECT_UNAVAILABLE, OutboundMailError.RECIPIENT_UNAVAILABLE]
)
def test_an_event_mail_skipped_because_its_subject_or_recipient_is_gone_is_no_failure(
    monkeypatch: pytest.MonkeyPatch, error: OutboundMailError
) -> None:
    healthy_plant(monkeypatch)
    ended_event_mail(OutboundMailStatus.SKIPPED, error)

    report, failed = printed(monkeypatch)
    assert [report[key] for key in MAIL_OUTCOMES] == [0, 0, 0]
    assert failed is False


def test_a_failed_event_mail_of_every_kind_fails_the_report_like_other_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy_plant(monkeypatch)
    ended_event_mail(OutboundMailStatus.FAILED, OutboundMailError.MAIL_REJECTED)

    report, failed = printed(monkeypatch)
    assert [report[key] for key in MAIL_OUTCOMES] == [0, len(EVENT_MAIL), 0]
    assert failed is True


def test_a_purge_warning_that_fails_an_item_fails_the_retention_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy_plant(monkeypatch)
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)

    def warning_fails(_: datetime) -> int:
        raise ItemsFailedError(1)

    monkeypatch.setattr(dispatcher, "warn_before_purge", warning_fails)
    assert dispatcher._run_phase("retention", dispatcher._retention) == ["retention"]

    report, failed = printed(monkeypatch)
    assert report["unhealthy_dispatch_phases"] == ["retention"]
    assert failed is True


def test_the_lag_argument_moves_the_overdue_and_dispatch_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    recent_forecast(system)
    dispatched(NOW - timedelta(minutes=7))
    CollectionJob.objects.create(
        binding=system.binding,
        target_date=NOW.date(),
        scheduled_for=NOW - timedelta(minutes=7),
        available_at=NOW - timedelta(minutes=7),
    )

    report, failed = printed(monkeypatch, "--max-lag-minutes", "5")
    assert report["overdue_collections"] == 1
    assert report["unhealthy_dispatch_phases"] == list(DispatcherPhase.values)
    assert failed is True

    report, failed = printed(monkeypatch, "--max-lag-minutes", "10")
    assert report["overdue_collections"] == 0
    assert report["unhealthy_dispatch_phases"] == []
    assert failed is False


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_the_whole_report_reads_a_fixed_number_of_statements_whatever_the_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    recent_forecast(system)
    dispatched()
    added = 0

    def seed(count: int) -> None:
        nonlocal added
        for _ in range(count):
            added += 1
            user = User.objects.create(
                username=f"owner-{added}", email=f"owner-{added}@example.com"
            )
            EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
            tenant = Tenant.objects.create(name=f"Tenant {added}")
            membership = Membership.objects.create(
                user=user, tenant=tenant, role=WorkspaceRole.ADMIN
            )
            region = Region.objects.create(
                tenant=tenant,
                name=f"Site {added}",
                latitude=40 + added / 100,
                longitude=30,
                timezone="Europe/Istanbul",
            )
            binding = SiteBinding.objects.create(
                region=region,
                provider_account=system.account,
                region_revision=1,
                site_id=f"site-{added}",
                capacity_kw=1,
                latitude=region.latitude,
                longitude=region.longitude,
                timezone=region.timezone,
                validated_at=NOW,
                validated_credential_revision=system.account.credential_revision,
            )
            Region.objects.filter(pk=region.pk).update(status="approved", approved_binding=binding)
            store(
                ReminderSchedule.objects.create(
                    region=region,
                    enabled=True,
                    effective_from=NOW,
                    include_workspace_members=True,
                ),
                [user.email],
                {user.email: [membership.pk]},
            )

    def report() -> None:
        printed(monkeypatch)

    assert_scale_invariant(seed, report, small=1, large=4)
    with captured_statements() as statements:
        report()
    # BEGIN, SET LOCAL statement_timeout, eleven reads and COMMIT.
    assert len(statements) <= 14
