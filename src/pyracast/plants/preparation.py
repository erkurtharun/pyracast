from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pyracast.forecasts.jobs import binding_is_current
from pyracast.irradiance import local_day_bounds
from pyracast.plants.availability import interval_is_covered, reference_intervals
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
    ProductionObservation,
)
from pyracast.plants.numerics import POLICY_VERSION, PowerInterval
from pyracast.regions.models import Region


def _matched_days(
    configuration: PlantConfiguration, observations: list[ProductionObservation], now: datetime
) -> list[date]:
    zone = ZoneInfo(configuration.binding.timezone)
    grouped: dict[date, list[ProductionObservation]] = defaultdict(list)
    for observation in observations:
        grouped[observation.starts_at.astimezone(zone).date()].append(observation)
    matched = []
    for day, rows in grouped.items():
        start, end = local_day_bounds(day, str(zone))
        measured = tuple(
            PowerInterval(
                row.starts_at,
                row.ends_at,
                row.energy_mwh / ((row.ends_at - row.starts_at).total_seconds() / 3600),
            )
            for row in rows
            if row.quality == "valid"
        )
        if interval_is_covered(measured, start, end) and interval_is_covered(
            reference_intervals(configuration, day, now), start, end
        ):
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
        observations = ProductionObservation.objects.filter(
            plant=plant,
            configuration=configuration,
            recorded_at__lte=moment,
            ends_at__lte=day_start,
        )
        latest = observations.order_by("-pk").first()
        if latest is None:
            return None
        last = observations.latest("ends_at")
        end_day = (
            (last.ends_at - timedelta(microseconds=1))
            .astimezone(ZoneInfo(configuration.binding.timezone))
            .date()
        )
        start, _ = local_day_bounds(end_day - timedelta(days=89), configuration.binding.timezone)
        rows = list(observations.filter(starts_at__gte=start).order_by("starts_at"))
        days = _matched_days(configuration, rows, moment)
        comparison = (
            CalibrationActivation.objects.filter(plant=plant, configuration=configuration)
            .order_by("-created_at", "-pk")
            .first()
        )
        compared = Q(comparison_activation=comparison)
        if comparison is not None and comparison.calibration_id is not None:
            compared |= Q(pk=comparison.calibration_id)
        if (
            len(days) < 7
            or CalibrationRun.objects.filter(
                compared,
                Q(status="failed") | Q(result__policy_version=POLICY_VERSION),
                configuration=configuration,
                observation_id_cutoff__gte=latest.pk,
                window_start__lte=days[0],
                window_end__gte=days[-1],
            ).exists()
        ):
            return None
        run = CalibrationRun.objects.create(
            plant=plant,
            configuration=configuration,
            binding=configuration.binding,
            comparison_activation=comparison,
            requested_by=latest.recorded_by,
            window_start=days[0],
            window_end=days[-1],
            cutoff_at=moment,
            observation_id_cutoff=latest.pk,
        )
        PlantEvent.objects.create(
            plant=plant,
            actor=latest.recorded_by,
            action="calibration_scheduled",
            details={"calibration_id": run.pk, "observation_id_cutoff": latest.pk},
        )
        return run
