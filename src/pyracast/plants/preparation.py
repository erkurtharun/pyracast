from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pyracast.forecasts.jobs import binding_is_current
from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasConnection
from pyracast.plants.availability import reference_intervals
from pyracast.plants.measurements import (
    Measurement,
    latest_measurement_day,
    select_measurements,
    selection_fingerprint,
)
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
    ProductionObservation,
)
from pyracast.plants.numerics import (
    POLICY_VERSION,
    CalibrationSample,
    PowerInterval,
    evaluate_samples,
)
from pyracast.regions.models import Region
from pyracast.solar import daylight_window


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
        if (
            plant.deleted_at is not None
            or configuration is None
            or not binding_is_current(configuration.binding)
        ):
            return None
        if CalibrationRun.objects.filter(plant=plant, status__in=("pending", "running")).exists():
            return None
        moment = timezone.now() if now is None else max(now, timezone.now())
        today = moment.astimezone(ZoneInfo(configuration.binding.timezone)).date()
        day_start, _ = local_day_bounds(today, configuration.binding.timezone)
        latest = (
            ProductionObservation.objects.filter(
                plant=plant,
                configuration=configuration,
                recorded_at__lte=moment,
                ends_at__lte=day_start,
            )
            .order_by("-pk")
            .first()
        )
        watermark = latest.pk if latest else 0
        end_day = latest_measurement_day(configuration, day_start, moment, watermark)
        if end_day is None:
            return None
        start, _ = local_day_bounds(end_day - timedelta(days=89), configuration.binding.timezone)
        _, end = local_day_bounds(end_day, configuration.binding.timezone)
        rows, snapshot_ids = select_measurements(
            configuration, start, end, moment, observation_id_cutoff=watermark
        )
        days = _matched_days(configuration, rows, moment)
        if len(days) < 7:
            return None
        window_start, _ = local_day_bounds(days[0], configuration.binding.timezone)
        _, window_end = local_day_bounds(days[-1], configuration.binding.timezone)
        rows, snapshot_ids = select_measurements(
            configuration, window_start, window_end, moment, observation_id_cutoff=watermark
        )
        fingerprint, reference_ids = selection_fingerprint(configuration, rows, moment)
        comparison = (
            CalibrationActivation.objects.filter(plant=plant, configuration=configuration)
            .order_by("-created_at", "-pk")
            .first()
        )
        compared = Q(comparison_activation=comparison)
        if comparison is not None and comparison.calibration_id is not None:
            compared |= Q(pk=comparison.calibration_id)
        previous = CalibrationRun.objects.filter(
            compared,
            Q(status="failed") | Q(result__policy_version=POLICY_VERSION),
            configuration=configuration,
            window_start__lte=days[0],
            window_end__gte=days[-1],
        )
        if previous.filter(input_fingerprint=fingerprint).exists() or (
            latest is not None
            and not snapshot_ids
            and previous.filter(input_fingerprint="", observation_id_cutoff__gte=watermark).exists()
        ):
            return None
        actor = (
            latest.recorded_by if latest else EpiasConnection.objects.get(plant=plant).connected_by
        )
        run = CalibrationRun.objects.create(
            plant=plant,
            configuration=configuration,
            binding=configuration.binding,
            comparison_activation=comparison,
            requested_by=actor,
            window_start=days[0],
            window_end=days[-1],
            cutoff_at=moment,
            observation_id_cutoff=watermark,
            settlement_snapshot_ids=snapshot_ids,
            reference_snapshot_ids=reference_ids,
            input_fingerprint=fingerprint,
        )
        PlantEvent.objects.create(
            plant=plant,
            actor=actor,
            action="calibration_scheduled",
            details={"calibration_id": run.pk, "observation_id_cutoff": watermark},
        )
        return run
