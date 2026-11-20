import logging
from calendar import monthrange
from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from functools import partial
from itertools import chain
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from pyracast.background.failures import BACK_OFF, Deferrals, isolate, log_failure
from pyracast.collection_window import nightly_window
from pyracast.epias import EpiasError, EpiasPlant
from pyracast.forecasts.models import CollectionSchedule
from pyracast.metering.client import get_epias_client
from pyracast.metering.models import (
    EpiasCollectionJob,
    EpiasConnection,
    EpiasJobStatus,
    EpiasSnapshot,
    EpiasSourceKind,
)
from pyracast.plants.models import Plant, PlantConfiguration, active_plant
from pyracast.plants.operations import configuration_is_current
from pyracast.regions.models import Region

MARKET_ZONE = ZoneInfo("Europe/Istanbul")
INITIAL_SETTLEMENT_MONTHS = 12
SETTLEMENT_RECHECK = timedelta(days=1)
SETTLEMENT_RETRY = timedelta(hours=1)
HORIZON_DAYS = 90
ROLLING_DAYS = 7
ROLLING_MONTHS = 3
# A slot whose collection keeps failing is repaired on at most this many nights, so a deterministic
# failure ends visibly; each repair spends at most MAX_ATTEMPTS requests.
REPAIR_NIGHTS = 3
REPAIRS_PER_NIGHT = 31

logger = logging.getLogger(__name__)
_deferrals = Deferrals(BACK_OFF)


def _daily_window(connection: EpiasConnection, now: datetime) -> tuple[datetime, datetime] | None:
    region = connection.plant.region
    schedule = CollectionSchedule.objects.filter(region=region).first()
    if schedule is not None and not schedule.enabled:
        return None
    window = nightly_window(now.astimezone(ZoneInfo(region.timezone)).date(), region.timezone)
    if (
        window is None
        or not window[0] <= now < window[1]
        or window[0] < connection.updated_at
        or (schedule and now < schedule.effective_from)
    ):
        return None
    return window


def _provider_plant(connection: EpiasConnection, kind: str) -> dict[str, object]:
    return {
        "id": connection.settlement_plant_id
        if kind == EpiasSourceKind.SETTLEMENT_INJECTION
        else connection.provider_plant_id,
        "eic": connection.eic,
        "name": connection.name,
        "short_name": "",
    }


def enqueue_connection(
    connection: EpiasConnection, now: datetime, *, initial: bool = False
) -> list[int]:
    plant = connection.plant
    configuration = plant.current_configuration
    if (
        not connection.enabled
        or configuration is None
        or not configuration_is_current(configuration)
    ):
        return []
    scheduled_for = max(connection.updated_at, configuration.created_at) if initial else now
    retry_until = None
    if scheduled_for > now:
        return []
    if initial:
        region = plant.region
        window = nightly_window(
            scheduled_for.astimezone(ZoneInfo(region.timezone)).date(), region.timezone
        )
        if (
            window is not None
            and window[0] <= scheduled_for <= now < window[1]
            and not CollectionSchedule.objects.filter(region=region, enabled=False).exists()
        ):
            retry_until = window[1]
    else:
        window = _daily_window(connection, now)
        if window is None:
            return []
        scheduled_for = window[0]
        retry_until = window[1]
    today = scheduled_for.astimezone(MARKET_ZONE).date()
    if not initial:
        existing_slot = (
            EpiasCollectionJob.objects.filter(
                connection=connection,
                connection_revision=connection.revision,
                configuration=configuration,
                target_date=today - timedelta(days=1),
                source_kind=EpiasSourceKind.REALTIME_GENERATION,
                scheduled_for__gte=scheduled_for,
                scheduled_for__lt=retry_until,
                retry_until=retry_until,
            )
            .order_by("scheduled_for", "pk")
            .values_list("scheduled_for", flat=True)
            .first()
        )
        if existing_slot is not None:
            scheduled_for = existing_slot
    jobs = []
    for offset in range(1, (HORIZON_DAYS if initial else ROLLING_DAYS) + 1):
        job, _ = EpiasCollectionJob.objects.get_or_create(
            connection=connection,
            plant=plant,
            connection_revision=connection.revision,
            configuration=configuration,
            target_date=today - timedelta(days=offset),
            source_kind=EpiasSourceKind.REALTIME_GENERATION,
            scheduled_for=scheduled_for,
            defaults={
                "provider_plant": _provider_plant(connection, EpiasSourceKind.REALTIME_GENERATION),
                "available_at": now,
                "retry_until": retry_until if not initial or offset == 1 else None,
            },
        )
        jobs.append(job.pk)
    if connection.settlement_plant_id is not None:
        month_end = today.replace(day=1) - timedelta(days=1)
        initial_settlement = not (
            EpiasCollectionJob.objects.filter(
                connection=connection,
                connection_revision=connection.revision,
                source_kind=EpiasSourceKind.SETTLEMENT_INJECTION,
            )
            .exclude(scheduled_for=scheduled_for)
            .exists()
        )
        minimum_months = (
            INITIAL_SETTLEMENT_MONTHS
            if initial or initial_settlement
            else 3
            if today.weekday() == 0
            else 2
        )
        offset = 0
        while offset < minimum_months:
            month_start = month_end.replace(day=1)
            job, _ = EpiasCollectionJob.objects.get_or_create(
                connection=connection,
                plant=plant,
                connection_revision=connection.revision,
                configuration=configuration,
                source_kind=EpiasSourceKind.SETTLEMENT_INJECTION,
                target_date=month_start,
                scheduled_for=scheduled_for,
                defaults={
                    "period_end_day": month_end,
                    "provider_plant": _provider_plant(
                        connection, EpiasSourceKind.SETTLEMENT_INJECTION
                    ),
                    "available_at": now,
                    "retry_until": None if initial else retry_until,
                },
            )
            jobs.append(job.pk)
            month_end = month_start - timedelta(days=1)
            offset += 1
    return jobs


def _horizon(connection: EpiasConnection, today: date) -> dict[tuple[str, date], date | None]:
    """The slots behind the rolling re-collection that a repair may collect, with their end."""
    slots: dict[tuple[str, date], date | None] = {
        (EpiasSourceKind.REALTIME_GENERATION, today - timedelta(days=offset)): None
        for offset in range(ROLLING_DAYS + 1, HORIZON_DAYS + 1)
    }
    if connection.settlement_plant_id is not None:
        month_end = today.replace(day=1) - timedelta(days=1)
        for offset in range(INITIAL_SETTLEMENT_MONTHS):
            month_start = month_end.replace(day=1)
            if offset >= ROLLING_MONTHS:
                slots[(EpiasSourceKind.SETTLEMENT_INJECTION, month_start)] = month_end
            month_end = month_start - timedelta(days=1)
    return slots


def _slot(source_kind: str, target_date: date) -> tuple[str, date]:
    if source_kind == EpiasSourceKind.SETTLEMENT_INJECTION:
        return source_kind, target_date.replace(day=1)
    return source_kind, target_date


def _slots(rows: Iterable[tuple[str, date]]) -> list[tuple[str, date]]:
    return [_slot(kind, target) for kind, target in rows]


def repair_connection(
    connection: EpiasConnection, configuration: PlantConfiguration, now: datetime
) -> list[int]:
    """Under tonight's slot, collect again each slot of the horizon that has no snapshot of this
    identity and configuration and no open job, newest first and within the caps."""
    window = _daily_window(connection, now)
    if window is None or not configuration_is_current(configuration):
        return []
    slot, retry_until = window
    horizon = _horizon(connection, slot.astimezone(MARKET_ZONE).date())
    oldest = min(target for _, target in horizon)
    jobs = EpiasCollectionJob.objects.filter(
        connection=connection,
        connection_revision=connection.revision,
        configuration=configuration,
        target_date__gte=oldest,
    )
    kinds_and_dates = ("source_kind", "target_date")
    tonight = set(_slots(jobs.filter(scheduled_for=slot).values_list(*kinds_and_dates)))
    busy = set(
        _slots(
            jobs.filter(status__in=(EpiasJobStatus.PENDING, EpiasJobStatus.RUNNING)).values_list(
                *kinds_and_dates
            )
        )
    )
    collected = set(
        _slots(
            EpiasSnapshot.objects.filter(
                job__in=jobs.values("pk"), target_date__gte=oldest
            ).values_list(*kinds_and_dates)
        )
    )
    failures = Counter(
        _slots(jobs.filter(status=EpiasJobStatus.FAILED).values_list(*kinds_and_dates))
    )
    candidates = sorted(
        (
            key
            for key in horizon.keys() - tonight - busy - collected
            if failures[key] <= REPAIR_NIGHTS
        ),
        key=lambda key: (key[1], key[0]),
        reverse=True,
    )
    for kind, target in candidates[: max(REPAIRS_PER_NIGHT - len(tonight & horizon.keys()), 0)]:
        EpiasCollectionJob.objects.get_or_create(
            connection=connection,
            plant_id=connection.plant_id,
            connection_revision=connection.revision,
            configuration=configuration,
            source_kind=kind,
            target_date=target,
            scheduled_for=slot,
            defaults={
                "period_end_day": horizon[(kind, target)],
                "provider_plant": _provider_plant(connection, kind),
                "available_at": now,
                "retry_until": retry_until,
            },
        )
    return [
        pk
        for pk, kind, target in jobs.filter(scheduled_for=slot)
        .order_by("source_kind", "-target_date", "pk")
        .values_list("pk", *kinds_and_dates)
        if (kind, target) in horizon
    ]


class _SettlementDirectory:
    """Downloads the EPIAS settlement plant list at most once per scheduler run."""

    def __init__(self, now: datetime) -> None:
        self._now = now
        self._plants: tuple[EpiasPlant, ...] | None = None
        self._failed = False

    def resolve(self, eic: str | None) -> tuple[int | None, datetime]:
        """Return the settlement id and check stamp; a failed list stamps a short retry."""
        plants = self._load()
        if plants is None:
            return None, self._now - SETTLEMENT_RECHECK + SETTLEMENT_RETRY
        matches = [plant.id for plant in plants if plant.eic == eic]
        return (matches[0] if len(matches) == 1 else None), self._now

    def _load(self) -> tuple[EpiasPlant, ...] | None:
        if self._plants is None and not self._failed:
            try:
                self._plants = get_epias_client().list_settlement_plants()
            except EpiasError as error:
                self._failed = True
                log_failure(logger, "epias_settlement_lookup_failed", error, code=error.args[0])
        return self._plants


def enqueue_due_epias_jobs(*, now: datetime | None = None) -> list[int]:
    now = now or timezone.now()
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    connections = list(
        EpiasConnection.objects.filter(active_plant("plant__"), enabled=True)
        .exclude(pk__in=_deferrals.active(now))
        .select_related("plant")
        .order_by("pk")
    )
    batches = isolate(
        connections,
        partial(_enqueue_due_connection, now=now, directory=_SettlementDirectory(now)),
        logger=logger,
        event="metering_connection_failed",
        name=lambda connection: f"connection:{connection.pk}",
        back_off=lambda connection: _deferrals.defer(connection.pk, now),
    )
    return list(dict.fromkeys(chain.from_iterable(batches)))


def _settlement_due(connection: EpiasConnection, now: datetime) -> bool:
    return (
        connection.settlement_plant_id is None
        and (
            connection.settlement_checked_at is None
            or connection.settlement_checked_at <= now - SETTLEMENT_RECHECK
        )
        and connection.plant.current_configuration is not None
        and configuration_is_current(connection.plant.current_configuration)
        and _daily_window(connection, now) is not None
    )


def _enqueue_due_connection(
    connection: EpiasConnection, now: datetime, directory: _SettlementDirectory
) -> list[int]:
    check_settlement = _settlement_due(connection, now)
    settlement_id, checked_at = (
        directory.resolve(connection.eic) if check_settlement else (None, now)
    )
    identifiers: list[int] = []
    with transaction.atomic():
        region = Region.objects.select_for_update().filter(pk=connection.plant.region_id).first()
        if region is None:
            return identifiers
        plant = Plant.objects.select_for_update().get(pk=connection.plant_id)
        current = EpiasConnection.objects.select_for_update().filter(pk=connection.pk).first()
        if current is None:
            return identifiers
        if (
            check_settlement
            and current.revision == connection.revision
            and current.eic == connection.eic
            and current.settlement_plant_id is None
        ):
            current.settlement_plant_id = settlement_id
            current.settlement_checked_at = checked_at
            current.save(update_fields=("settlement_plant_id", "settlement_checked_at"))
        configuration = plant.current_configuration
        if configuration is not None and current.enabled:
            initial_slot = max(current.updated_at, configuration.created_at)
            initial_day = initial_slot.astimezone(MARKET_ZONE).date()
            oldest_day = initial_day - timedelta(days=90)
            first_month = initial_day.year * 12 + initial_day.month - 1 - INITIAL_SETTLEMENT_MONTHS
            year, month = divmod(first_month, 12)
            oldest_month = date(year, month + 1, 1)
            oldest_month_end = date(year, month + 1, monthrange(year, month + 1)[1])
            missing_settlement = (
                connection.settlement_plant_id is not None
                and not EpiasCollectionJob.objects.filter(
                    connection=current,
                    connection_revision=current.revision,
                    configuration=configuration,
                    source_kind=EpiasSourceKind.SETTLEMENT_INJECTION,
                    target_date=oldest_month,
                    period_end_day=oldest_month_end,
                    provider_plant__id=current.settlement_plant_id,
                    provider_plant__eic=current.eic,
                ).exists()
            )
            if (
                missing_settlement
                or not EpiasCollectionJob.objects.filter(
                    connection=current,
                    connection_revision=current.revision,
                    configuration=configuration,
                    source_kind=EpiasSourceKind.REALTIME_GENERATION,
                    target_date=oldest_day,
                    scheduled_for=initial_slot,
                ).exists()
            ):
                identifiers.extend(enqueue_connection(current, now, initial=True))
        identifiers.extend(enqueue_connection(current, now))
        if configuration is not None and current.enabled:
            identifiers.extend(repair_connection(current, configuration, now))
    return identifiers
