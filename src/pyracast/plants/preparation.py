import logging
from collections import defaultdict
from collections.abc import Iterable
from contextlib import suppress
from datetime import date, datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

from django.db import models, transaction
from django.db.models import Exists, F, OuterRef, Q, Value
from django.db.models.functions import Coalesce, Least
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.background.failures import ItemsFailedError, isolate
from pyracast.connections.currency import binding_is_current
from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasConnection
from pyracast.plants.availability import reference_intervals
from pyracast.plants.calibration_runs import (
    OPEN_STATUSES,
    SupersedeReason,
    create_run,
    current_activation,
    finish_run,
    pin_inputs,
)
from pyracast.plants.incident_policy import incidents_unchanged
from pyracast.plants.measurements import (
    Measurement,
    latest_measurement_day,
    select_measurements,
)
from pyracast.plants.models import (
    CalibrationError,
    CalibrationRun,
    CalibrationStatus,
    Plant,
    PlantConfiguration,
    ProductionObservation,
    active_plant,
)
from pyracast.plants.numerics import (
    POLICY_VERSION,
    CalibrationSample,
    PowerInterval,
    evaluate_samples,
)
from pyracast.regions.models import Region
from pyracast.solar import daylight_window

AUTOMATIC_POOL_DAYS = 60
PREPARATION_RETRY_DELAY = timedelta(hours=1)
# A run that failed without a data error is retried by the next preparation after this delay.
FAILED_RUN_RETRY_DELAY = timedelta(days=1)

logger = logging.getLogger(__name__)


def _matched_days(
    configuration: PlantConfiguration, observations: list[Measurement], now: datetime
) -> list[date]:
    zone = ZoneInfo(configuration.binding.timezone)
    grouped: dict[date, list[Measurement]] = defaultdict(list)
    for observation in observations:
        grouped[observation.starts_at.astimezone(zone).date()].append(observation)
    matched = []
    for day, rows in grouped.items():
        binding = configuration.binding
        windows = daylight_window(day, str(zone), binding.latitude, binding.longitude).windows
        source = reference_intervals(configuration, day, now)
        evaluation = evaluate_samples(
            tuple(
                CalibrationSample(
                    row.starts_at,
                    row.ends_at,
                    row.energy_mwh,
                    tuple(
                        PowerInterval(
                            max(piece.start, row.starts_at),
                            min(piece.end, row.ends_at),
                            piece.power_mw,
                        )
                        for piece in source
                        if piece.start < row.ends_at and piece.end > row.starts_at
                    ),
                    row.quality,
                )
                for row in rows
            ),
            timezone=str(zone),
            limit_mw=None,
            required_windows={day: windows},
        )
        if evaluation["valid_days"] == 1:
            matched.append(day)
    return sorted(matched)


def prepare_calibration(plant_id: int, now: datetime | None = None) -> CalibrationRun | None:
    region_id = Plant.objects.values_list("region_id", flat=True).get(pk=plant_id)
    with transaction.atomic():
        Region.objects.select_for_update().get(pk=region_id)
        plant = (
            Plant.objects.select_for_update(of=("self",))
            .select_related("current_configuration__binding__provider_account")
            .get(pk=plant_id)
        )
        configuration = plant.current_configuration
        if configuration is None or not binding_is_current(configuration.binding):
            return None
        if CalibrationRun.objects.filter(plant=plant, status__in=OPEN_STATUSES).exists():
            return None
        moment = timezone.now() if now is None else max(now, timezone.now())
        today = moment.astimezone(ZoneInfo(configuration.binding.timezone)).date()
        day_start, _ = local_day_bounds(today, configuration.binding.timezone)
        latest = (
            ProductionObservation.objects.filter(
                plant=plant,
                configuration=configuration,
                created_at__lte=moment,
                ends_at__lte=day_start,
            )
            .order_by("-pk")
            .first()
        )
        watermark = latest.pk if latest else 0
        end_day = latest_measurement_day(configuration, day_start, moment, watermark)
        if end_day is None:
            return None
        start, _ = local_day_bounds(
            end_day - timedelta(days=AUTOMATIC_POOL_DAYS - 1), configuration.binding.timezone
        )
        _, end = local_day_bounds(end_day, configuration.binding.timezone)
        rows, _ = select_measurements(
            configuration, start, end, moment, observation_id_cutoff=watermark
        )
        days = _matched_days(configuration, rows, moment)
        if not days:
            return None
        inputs = pin_inputs(configuration, days[0], days[-1], moment, watermark)
        comparison = current_activation(plant, configuration)
        compared = Q(comparison_activation=comparison)
        if comparison is not None and comparison.calibration_id is not None:
            compared |= Q(pk=comparison.calibration_id)
        previous = CalibrationRun.objects.filter(
            compared,
            Q(status=CalibrationStatus.FAILED, last_error=CalibrationError.INVALID_CALIBRATION_DATA)
            | Q(status=CalibrationStatus.FAILED, completed_at__gt=moment - FAILED_RUN_RETRY_DELAY)
            | Q(result__policy_version=POLICY_VERSION),
            configuration=configuration,
            window_start__lte=days[0],
            window_end__gte=days[-1],
            incident_revision_ids=inputs.incident_revision_ids,
        )
        if previous.filter(input_sha256=inputs.input_sha256).exists() or (
            latest is not None
            and not inputs.production_snapshot_ids
            and previous.filter(input_sha256="", observation_id_cutoff__gte=watermark).exists()
        ):
            return None
        actor = (
            latest.recorded_by if latest else EpiasConnection.objects.get(plant=plant).connected_by
        )
        return create_run(
            plant, configuration, inputs, comparison=comparison, requested_by=actor, manual=False
        )


def request_preparation(plant_ids: Iterable[int], now: datetime | None = None) -> None:
    """Record in the caller's transaction that these plants need preparation.

    One attempt runs after commit; the calibration dispatch phase prepares what is left.
    """
    identifiers = list(plant_ids)
    if not identifiers:
        return
    moment = Value(now or timezone.now(), output_field=models.DateTimeField())
    Plant.objects.filter(pk__in=identifiers).update(
        calibration_preparation_due_at=Least(
            Coalesce(F("calibration_preparation_due_at"), moment), moment
        )
    )
    transaction.on_commit(partial(_prepare_after_commit, identifiers, now))


def incidents_changed(plant_id: int) -> None:
    """Supersede open runs whose outage inputs changed; the caller holds the plant lock."""
    moment = timezone.now()
    for run in CalibrationRun.objects.filter(
        plant_id=plant_id, status__in=OPEN_STATUSES
    ).select_related("configuration", "binding"):
        if not incidents_unchanged(run, moment):
            finish_run(
                run, CalibrationStatus.SUPERSEDED, moment, SupersedeReason.OUTAGE_HISTORY_CHANGED
            )
    request_preparation([plant_id], moment)


def request_region_preparation(region_id: int) -> None:
    request_preparation(
        Plant.objects.filter(region_id=region_id, current_configuration__isnull=False).values_list(
            "pk", flat=True
        )
    )


def _prepare_after_commit(plant_ids: list[int], now: datetime | None) -> None:
    with suppress(ItemsFailedError):
        _prepare_each(plant_ids, now)


def prepare_requested_calibrations(now: datetime, limit: int = 100) -> list[int]:
    due = (
        Plant.objects.filter(active_plant(), calibration_preparation_due_at__lte=now)
        .exclude(
            Exists(CalibrationRun.objects.filter(plant=OuterRef("pk"), status__in=OPEN_STATUSES))
        )
        .order_by("calibration_preparation_due_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    return [run.pk for run in _prepare_each(list(due), now) if run is not None]


def _prepare_each(plant_ids: list[int], now: datetime | None) -> list[CalibrationRun | None]:
    return isolate(
        plant_ids,
        partial(_prepare_requested, now=now),
        logger=logger,
        event="calibration_preparation_failed",
        name=lambda plant_id: f"plant:{plant_id}",
        back_off=partial(_defer_request, now=now),
    )


def _prepare_requested(plant_id: int, now: datetime | None) -> CalibrationRun | None:
    from pyracast.plants.automation import promote_undecided_candidate

    moment = now or timezone.now()
    with transaction.atomic():
        identity = (
            Plant.objects.filter(pk=plant_id).values_list("region_id", "region__tenant_id").first()
        )
        if identity is None:
            return None
        Tenant.objects.select_for_update().get(pk=identity[1])
        Region.objects.select_for_update().get(pk=identity[0])
        due_at = (
            Plant.objects.select_for_update()
            .filter(pk=plant_id)
            .values_list("calibration_preparation_due_at", flat=True)
            .first()
        )
        if (
            due_at is None
            or due_at > moment
            or CalibrationRun.objects.filter(plant_id=plant_id, status__in=OPEN_STATUSES).exists()
        ):
            return None
        run = prepare_calibration(plant_id, now)
        settled = run is not None or promote_undecided_candidate(plant_id, moment)
        Plant.objects.filter(pk=plant_id).update(
            calibration_preparation_due_at=None if settled else moment + PREPARATION_RETRY_DELAY
        )
        return run


def _defer_request(plant_id: int, now: datetime | None) -> None:
    Plant.objects.filter(pk=plant_id, calibration_preparation_due_at__isnull=False).update(
        calibration_preparation_due_at=(now or timezone.now()) + PREPARATION_RETRY_DELAY
    )
