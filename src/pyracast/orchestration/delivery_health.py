import logging
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import reduce
from operator import or_
from typing import Any, Literal

from django.db.models import Count, Exists, F, OuterRef, Q, QuerySet, Subquery, Value
from django.db.models.functions import Coalesce, Greatest, Least
from django.db.models.lookups import LessThan

from pyracast.accounts.models import OutboundMail, OutboundMailStatus
from pyracast.background.broker import QueueState, queue_states
from pyracast.background.deadlines import HEARTBEAT_LAG
from pyracast.background.failures import log_failure
from pyracast.background.lease import claimable, holding
from pyracast.forecasts.jobs import ready as collection_jobs
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobStatus,
    DispatcherPhase,
    DispatcherStatus,
    ForecastSnapshot,
    RequestAttempt,
    SnapshotSourceKind,
)
from pyracast.mail.errors import CREDENTIAL_FAILURES
from pyracast.metering import jobs as metering_jobs
from pyracast.metering.models import EpiasCollectionJob, EpiasJobStatus
from pyracast.notifications import jobs as reminder_jobs
from pyracast.notifications import outbox
from pyracast.notifications.models import (
    PAUSED_DELIVERY_ERRORS,
    NotificationDelivery,
    NotificationDeliveryError,
    NotificationDeliveryStatus,
    PreparedMessageKind,
)
from pyracast.notifications.operations import invalid_recipient_region_ids
from pyracast.orchestration.kinds import QUEUES
from pyracast.plants import training
from pyracast.plants.models import CalibrationRun, CalibrationStatus, active_plant
from pyracast.regions.models import Region, RegionStatus, active_region

logger = logging.getLogger(__name__)

# The console uses the dispatcher's heartbeat lag; the command takes it as its default.
DISPATCH_LAG = HEARTBEAT_LAG
RECENT_WINDOW = timedelta(days=1)
STALE_FORECAST_AFTER = timedelta(hours=26)
REQUEST_DAYS = 7

type Scope = Literal["plants", "collections", "deliveries", "mail", "metering", "calibrations"]


@dataclass(frozen=True)
class Window:
    now: datetime
    lag: timedelta

    @property
    def cutoff(self) -> datetime:
        return self.now - self.lag

    @property
    def recent(self) -> datetime:
        return self.now - RECENT_WINDOW


@dataclass(frozen=True)
class Check:
    key: str
    scope: Scope
    condition: Callable[[Window], Q]
    alarm: bool = True
    # Counted once per provider account, listed for every plant on the account.
    per_account: bool = False


def _plants() -> QuerySet[Region]:
    forecasts = (
        ForecastSnapshot.objects.filter(
            binding_id=OuterRef("approved_binding_id"),
            source_kind=SnapshotSourceKind.FORECAST,
            job__status=CollectionJobStatus.SUCCEEDED,
            forecastresult__isnull=False,
        )
        .order_by("-collected_at")
        .values("collected_at")[:1]
    )
    open_forecast = CollectionJob.objects.filter(
        binding_id=OuterRef("approved_binding_id"),
        endpoint=CollectionEndpoint.FORECASTS,
        status__in=(*collection_jobs.WAITING, CollectionJobStatus.RUNNING),
    )
    return (
        Region.objects.active()
        .filter(status=RegionStatus.APPROVED, approved_binding__provider_account__enabled=True)
        .annotate(latest_forecast=Subquery(forecasts), forecast_open=Exists(open_forecast))
    )


TABLES: Mapping[Scope, Callable[[], QuerySet[Any]]] = {
    "plants": _plants,
    "collections": lambda: CollectionJob.objects.filter(active_region("binding__region__")),
    "deliveries": lambda: NotificationDelivery.objects.defer("prepared_message").filter(
        active_region("schedule__region__")
    ),
    "mail": OutboundMail.objects.all,
    "metering": lambda: EpiasCollectionJob.objects.filter(active_plant("plant__")),
    "calibrations": lambda: CalibrationRun.objects.filter(active_plant("plant__")),
}
ACCOUNT = "approved_binding__provider_account__"


def _collecting() -> Q:
    return ~Q(collectionschedule__enabled=False)


def _stale(window: Window) -> Q:
    # A plant whose first forecast is still being collected is watched by the job checks.
    return _collecting() & (
        Q(latest_forecast__lt=window.now - STALE_FORECAST_AFTER)
        | Q(latest_forecast__isnull=True, forecast_open=False)
    )


def _invalid_recipients(_: Window) -> Q:
    # An empty IN () would empty the whole aggregate statement; a primary key is never null.
    return Q(pk__in=ids) if (ids := invalid_recipient_region_ids()) else Q(pk__isnull=True)


def _calibration_claimable_at() -> Greatest:
    # A run has no due time. The claim waits for a cutoff ahead and fails a run whose cutoff lies
    # more than one lease ahead, so a run is claimable from its cutoff, at most a lease after its
    # creation, and never before it waits (since its creation or its latest requeue).
    return Greatest(
        Coalesce("available_at", "created_at"),
        Least("cutoff_at", F("created_at") + Value(training.LEASE_DURATION)),
    )


def _recent(window: Window, **lookups: object) -> Q:
    return Q(scheduled_for__gte=window.recent, **lookups)


CHECKS: tuple[Check, ...] = (
    Check(
        "invalid_reminder_recipients",
        "plants",
        _invalid_recipients,
    ),
    Check(
        "unvalidated_provider_credentials",
        "plants",
        lambda _: (
            _collecting()
            & ~Q(approved_binding__validated_credential_revision=F(f"{ACCOUNT}credential_revision"))
        ),
    ),
    Check(
        "blocked_provider_credentials",
        "plants",
        lambda _: Q(
            **{f"{ACCOUNT}authentication_failed_revision": F(f"{ACCOUNT}credential_revision")}
        ),
        per_account=True,
    ),
    Check("regions_without_recent_forecasts", "plants", _stale),
    Check(
        "provider_cooldowns",
        "plants",
        lambda window: Q(**{f"{ACCOUNT}request_blocked_until__gt": window.now}),
        per_account=True,
    ),
    Check(
        "overdue_collections",
        "collections",
        lambda window: Q(status__in=collection_jobs.WAITING, available_at__lt=window.cutoff),
    ),
    Check(
        "interrupted_collections",
        "collections",
        lambda window: claimable(CollectionJobStatus.RUNNING, window.now),
    ),
    Check(
        "failed_collections",
        "collections",
        lambda window: _recent(window, status=CollectionJobStatus.FAILED),
    ),
    Check(
        "overdue_notifications",
        "deliveries",
        lambda window: Q(status__in=reminder_jobs.WAITING, available_at__lt=window.cutoff),
    ),
    Check(
        "interrupted_notifications",
        "deliveries",
        lambda window: claimable(NotificationDeliveryStatus.SENDING, window.now),
    ),
    Check(
        "failed_notifications",
        "deliveries",
        lambda window: _recent(
            window,
            status__in=(NotificationDeliveryStatus.FAILED, NotificationDeliveryStatus.UNCERTAIN),
        ),
    ),
    Check(
        "missed_notifications",
        "deliveries",
        lambda window: _recent(
            window,
            status__in=(NotificationDeliveryStatus.SKIPPED, NotificationDeliveryStatus.FAILED),
            last_error__in=(
                NotificationDeliveryError.MISSED_SCHEDULE,
                NotificationDeliveryError.DELIVERY_DEADLINE_EXCEEDED,
            ),
        ),
    ),
    Check(
        "unavailable_forecasts",
        "deliveries",
        lambda window: _recent(window, forecast_kind=PreparedMessageKind.UNAVAILABLE),
    ),
    Check(
        "partial_forecasts",
        "deliveries",
        lambda window: _recent(window, forecast_complete=False),
    ),
    Check(
        "mail_authentication_failures",
        "deliveries",
        lambda window: _recent(window, last_error__in=CREDENTIAL_FAILURES),
    ),
    Check(
        "saved_forecast_notifications",
        "deliveries",
        lambda window: _recent(
            window,
            forecast_kind__in=(
                PreparedMessageKind.SAVED_COMPLETE,
                PreparedMessageKind.SAVED_PARTIAL,
            ),
        ),
        alarm=False,
    ),
    # A customer's own empty, invalid or over-limit list pauses its deliveries; that is no
    # operator alarm.
    Check(
        "paused_notifications",
        "deliveries",
        lambda window: _recent(
            window,
            status=NotificationDeliveryStatus.SKIPPED,
            last_error__in=PAUSED_DELIVERY_ERRORS,
        ),
        alarm=False,
    ),
    Check(
        "overdue_outbound_mail",
        "mail",
        lambda window: (
            Q(status__in=outbox.WAITING, available_at__lt=window.cutoff)
            | claimable(OutboundMailStatus.SENDING, window.now)
        ),
    ),
    Check(
        "failed_outbound_mail",
        "mail",
        lambda window: Q(status=OutboundMailStatus.FAILED, finished_at__gte=window.recent),
    ),
    Check(
        "uncertain_outbound_mail",
        "mail",
        lambda window: Q(status=OutboundMailStatus.UNCERTAIN, finished_at__gte=window.recent),
    ),
    Check(
        "overdue_metering",
        "metering",
        lambda window: Q(status__in=metering_jobs.WAITING, available_at__lt=window.cutoff),
    ),
    Check(
        "interrupted_metering",
        "metering",
        lambda window: claimable(EpiasJobStatus.RUNNING, window.now),
    ),
    Check(
        "failed_metering",
        "metering",
        lambda window: _recent(window, status=EpiasJobStatus.FAILED),
    ),
    Check(
        "queued_calibrations",
        "calibrations",
        lambda _: Q(status__in=training.WAITING),
        alarm=False,
    ),
    Check(
        "running_calibrations",
        "calibrations",
        lambda window: holding(CalibrationStatus.RUNNING, window.now),
        alarm=False,
    ),
    Check(
        "overdue_calibrations",
        "calibrations",
        lambda window: Q(
            LessThan(_calibration_claimable_at(), window.cutoff),
            status__in=training.WAITING,
        ),
    ),
    Check(
        "interrupted_calibrations",
        "calibrations",
        lambda window: claimable(CalibrationStatus.RUNNING, window.now),
    ),
    Check(
        "failed_calibrations",
        "calibrations",
        lambda window: Q(status=CalibrationStatus.FAILED, completed_at__gte=window.recent),
    ),
)
_BY_KEY = {item.key: item for item in CHECKS}


def check(key: str) -> Check:
    return _BY_KEY[key]


def listed(item: Check, window: Window) -> QuerySet[Any]:
    return TABLES[item.scope]().filter(item.condition(window))


def _counts(window: Window) -> dict[str, int]:
    counts: dict[str, int] = {}
    for scope, table in TABLES.items():
        conditions = {item: item.condition(window) for item in CHECKS if item.scope == scope}
        counts |= (
            table()
            .filter(reduce(or_, conditions.values()))
            .aggregate(
                **{
                    item.key: Count(
                        f"{ACCOUNT}id" if item.per_account else "pk",
                        filter=condition,
                        distinct=item.per_account,
                    )
                    for item, condition in conditions.items()
                }
            )
        )
    return {item.key: counts[item.key] for item in CHECKS}


@dataclass(frozen=True)
class PhaseState:
    phase: DispatcherPhase
    attempted_at: datetime | None
    succeeded_at: datetime | None
    last_error: str
    duration_ms: int | None
    published: int | None
    healthy: bool


def _phases(window: Window) -> tuple[PhaseState, ...]:
    rows = {row.phase: row for row in DispatcherStatus.objects.all()}
    states = []
    for phase in DispatcherPhase:
        row = rows.get(phase)
        if row is None:
            states.append(PhaseState(phase, None, None, "", None, None, healthy=False))
            continue
        healthy = (
            row.succeeded_at is not None
            and row.succeeded_at >= window.cutoff
            and not row.last_error
        )
        states.append(
            PhaseState(
                phase,
                row.attempted_at,
                row.succeeded_at,
                row.last_error,
                row.duration_ms,
                row.published,
                healthy,
            )
        )
    return tuple(states)


@dataclass(frozen=True)
class BindingRequests:
    binding_id: int
    today: int
    busiest_day: int


def _requests(window: Window) -> tuple[BindingRequests, ...]:
    today = window.now.astimezone(UTC).date()
    days: dict[int, dict[date, int]] = defaultdict(dict)
    for row in (
        RequestAttempt.objects.filter(
            binding__isnull=False,
            utc_day__gt=today - timedelta(days=REQUEST_DAYS),
            utc_day__lte=today,
        )
        .values("binding_id", "utc_day")
        .annotate(requests=Count("pk"))
        .order_by()
    ):
        days[row["binding_id"]][row["utc_day"]] = row["requests"]
    return tuple(
        sorted(
            (
                BindingRequests(binding, counts.get(today, 0), max(counts.values()))
                for binding, counts in days.items()
            ),
            key=lambda item: (-item.busiest_day, -item.today, item.binding_id),
        )
    )


@dataclass(frozen=True)
class BrokerState:
    reachable: bool
    queues: tuple[QueueState, ...] = ()

    def as_json(self) -> dict[str, object]:
        return {
            "reachable": self.reachable,
            "queues": {
                state.queue: {
                    "messages": state.messages,
                    "consumers": state.consumers,
                    "dead_letters": state.dead_letters,
                    "missing": state.missing,
                    "dead_letters_missing": state.dead_letters is None,
                }
                for state in self.queues
            },
        }


def read_broker() -> BrokerState:
    """The queue counts are a diagnostic; a broker that does not answer or a work queue the broker
    has not declared fails the report."""
    try:
        return BrokerState(True, queue_states(QUEUES))
    except Exception as error:
        log_failure(logger, "health_broker_unreadable", error)
        return BrokerState(False)


@dataclass(frozen=True)
class HealthReport:
    checked_at: datetime
    phases: tuple[PhaseState, ...]
    counts: Mapping[str, int]
    requests: tuple[BindingRequests, ...]
    broker: BrokerState | None = None

    @property
    def unhealthy_phases(self) -> list[str]:
        return [state.phase.value for state in self.phases if not state.healthy]

    @property
    def broker_unreachable(self) -> bool:
        # The dispatcher renews its heartbeat only after the broker answered and is restarted
        # within BROKER_OUTAGE_RESTART otherwise; its phases may still record success.
        return self.broker is not None and not self.broker.reachable

    @property
    def missing_queues(self) -> list[str]:
        # Workers and the dispatcher declare a work queue together with its dead-letter queue, so
        # a missing work queue has no consumer; a dead-letter queue alone holds no waiting work.
        return [] if self.broker is None else [s.queue for s in self.broker.queues if s.missing]

    @property
    def healthy(self) -> bool:
        return (
            not self.unhealthy_phases
            and not self.broker_unreachable
            and not self.missing_queues
            and not self.alarms
        )

    @property
    def alarms(self) -> dict[str, int]:
        return {
            item.key: self.counts[item.key]
            for item in CHECKS
            if item.alarm and self.counts[item.key]
        }

    def as_json(self) -> dict[str, object]:
        report: dict[str, object] = {
            "healthy": self.healthy,
            "checked_at": self.checked_at.isoformat(),
            "unhealthy_dispatch_phases": self.unhealthy_phases,
            "alarms": self.alarms,
            **self.counts,
            "provider_requests_max_binding_today": max(
                (item.today for item in self.requests), default=0
            ),
            "provider_requests_max_binding_day": max(
                (item.busiest_day for item in self.requests), default=0
            ),
        }
        if self.broker is not None:
            report["broker"] = self.broker.as_json()
        return report


def delivery_health(
    now: datetime,
    lag: timedelta = DISPATCH_LAG,
    *,
    broker: BrokerState | None = None,
) -> HealthReport:
    window = Window(now, lag)
    return HealthReport(
        checked_at=now,
        phases=_phases(window),
        counts=_counts(window),
        requests=_requests(window),
        broker=broker,
    )
