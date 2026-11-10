from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from pyracast.accounts.operations import require_member
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.history import HISTORY_SELECTION_POLICY
from pyracast.forecasts.jobs import binding_is_current
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
    ProductionObservation,
)
from pyracast.regions.models import Region


@dataclass(frozen=True)
class ObservationInput:
    starts_at: datetime
    ends_at: datetime
    energy: float
    quality: str = "valid"
    source: str = "operator"


def configuration_is_current(configuration: PlantConfiguration) -> bool:
    return Plant.objects.filter(
        pk=configuration.plant_id,
        current_configuration_id=configuration.pk,
        region_id=configuration.binding.region_id,
    ).exists() and binding_is_current(configuration.binding)


def _binding(region: Region) -> SiteBinding:
    if region.approved_binding_id is None:
        raise ValidationError("An approved current provider binding is required.")
    binding = SiteBinding.objects.select_related("provider_account").get(
        pk=region.approved_binding_id, region=region
    )
    if not binding_is_current(binding):
        raise ValidationError("An approved current provider binding is required.")
    return binding


def _configuration(plant: Plant) -> PlantConfiguration:
    if plant.current_configuration_id is None:
        raise ValidationError("configuration_unavailable")
    configuration = PlantConfiguration.objects.select_related("binding__provider_account").get(
        pk=plant.current_configuration_id, plant=plant
    )
    if not configuration_is_current(configuration):
        raise ValidationError("The plant configuration is no longer current.")
    return configuration


def _validate_configuration(
    name: str,
    dc_capacity_mw: float,
    inverter_capacity_mw: float | None,
    export_limit_mw: float | None,
    measurement_basis: str,
) -> None:
    if not name.strip() or len(name.strip()) > 200:
        raise ValidationError("A plant name of at most 200 characters is required.")
    for value in (dc_capacity_mw, inverter_capacity_mw, export_limit_mw):
        if value is not None and (isinstance(value, bool) or not isfinite(value) or value <= 0):
            raise ValidationError("Capacities must be finite and positive.")
    if measurement_basis not in {"net_export", "ac_generation"}:
        raise ValidationError("Unsupported meter basis.")


def _save_configuration(
    actor: User,
    plant: Plant,
    binding: SiteBinding,
    *,
    name: str,
    dc_capacity_mw: float,
    inverter_capacity_mw: float | None,
    export_limit_mw: float | None,
    measurement_basis: str,
) -> None:
    revision = 1
    if plant.current_configuration_id is not None:
        revision = PlantConfiguration.objects.get(pk=plant.current_configuration_id).revision + 1
    configuration = PlantConfiguration.objects.create(
        plant=plant,
        binding=binding,
        revision=revision,
        dc_capacity_mw=dc_capacity_mw,
        inverter_capacity_mw=inverter_capacity_mw,
        export_limit_mw=export_limit_mw,
        measurement_basis=measurement_basis,
        created_by=actor,
    )
    plant.name = name.strip()
    plant.current_configuration = configuration
    plant.save(update_fields=("name", "current_configuration"))
    CalibrationRun.objects.filter(plant=plant, status__in=("pending", "running")).update(
        status="superseded", completed_at=timezone.now(), lease_token=None, lease_expires_at=None
    )
    PlantEvent.objects.create(
        plant=plant,
        actor=actor,
        action="configuration_created",
        details={"configuration_id": configuration.pk},
    )


def create_plant(
    actor: User,
    tenant_id: int,
    region_id: int,
    *,
    name: str,
    dc_capacity_mw: float,
    inverter_capacity_mw: float | None = None,
    export_limit_mw: float | None = None,
    measurement_basis: str = "net_export",
) -> Plant:
    require_member(actor, tenant_id)
    _validate_configuration(
        name, dc_capacity_mw, inverter_capacity_mw, export_limit_mw, measurement_basis
    )
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=region_id, tenant_id=tenant_id)
        binding = _binding(region)
        plant = Plant.objects.create(region=region, name=name.strip())
        _save_configuration(
            actor,
            plant,
            binding,
            name=name,
            dc_capacity_mw=dc_capacity_mw,
            inverter_capacity_mw=inverter_capacity_mw,
            export_limit_mw=export_limit_mw,
            measurement_basis=measurement_basis,
        )
        return plant


def revise_plant(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    *,
    name: str,
    dc_capacity_mw: float,
    inverter_capacity_mw: float | None = None,
    export_limit_mw: float | None = None,
    measurement_basis: str = "net_export",
) -> Plant:
    require_member(actor, tenant_id)
    _validate_configuration(
        name, dc_capacity_mw, inverter_capacity_mw, export_limit_mw, measurement_basis
    )
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.select_for_update().get(pk=plant_id, region=region)
        _save_configuration(
            actor,
            plant,
            _binding(region),
            name=name,
            dc_capacity_mw=dc_capacity_mw,
            inverter_capacity_mw=inverter_capacity_mw,
            export_limit_mw=export_limit_mw,
            measurement_basis=measurement_basis,
        )
        return plant


def _observation(
    observation: ObservationInput, unit: str, zone: ZoneInfo, now: datetime
) -> tuple[datetime, datetime, float, str, str]:
    start, end = observation.starts_at, observation.ends_at
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValidationError("Observation timestamps must be timezone-aware.")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end - start not in {timedelta(minutes=30), timedelta(minutes=60)} or end > now:
        raise ValidationError("Completed 30 or 60 minute observations are required.")
    local = start.astimezone(zone)
    for stamp in (local, end.astimezone(zone)):
        if stamp.minute not in (0, 30) or stamp.second or stamp.microsecond:
            raise ValidationError("Observations must align to local half-hour boundaries.")
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), zone)
    if end > midnight.astimezone(UTC):
        raise ValidationError("Observations must not cross local midnight.")
    value = observation.energy
    if isinstance(value, bool) or not isfinite(value) or value < 0:
        raise ValidationError("Observed energy must be finite and nonnegative.")
    if observation.quality not in {"valid", "outage", "curtailed", "invalid"}:
        raise ValidationError("Unsupported observation quality.")
    source = observation.source.strip()
    if not source or len(source) > 200:
        raise ValidationError("An observation source of at most 200 characters is required.")
    return start, end, value / 1000 if unit == "kWh" else value, observation.quality, source


def import_observations(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    *,
    configuration_id: int,
    unit: str,
    observations: list[ObservationInput],
) -> dict[str, object]:
    require_member(actor, tenant_id)
    if unit not in {"MWh", "kWh"} or not 1 <= len(observations) <= 2000:
        raise ValidationError("Use MWh or kWh and a batch of 1 to 2000 observations.")
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.select_for_update().get(pk=plant_id, region=region)
        configuration = PlantConfiguration.objects.select_related("binding").get(
            pk=configuration_id, plant=plant
        )
        now, zone = timezone.now(), ZoneInfo(configuration.binding.timezone)
        created = 0
        ids: list[int] = []
        for item in observations:
            start, end, value, quality, source = _observation(item, unit, zone, now)
            overlap = ProductionObservation.objects.filter(
                plant=plant, starts_at__lt=end, ends_at__gt=start
            ).first()
            if overlap is not None:
                if (
                    overlap.starts_at != start
                    or overlap.ends_at != end
                    or overlap.configuration_id != configuration.pk
                    or overlap.energy_mwh != value
                    or overlap.quality != quality
                    or overlap.source != source
                ):
                    raise ValidationError("Conflicting or overlapping observation.")
                ids.append(overlap.pk)
                continue
            saved = ProductionObservation.objects.create(
                plant=plant,
                configuration=configuration,
                starts_at=start,
                ends_at=end,
                energy_mwh=value,
                quality=quality,
                source=source,
                recorded_by=actor,
            )
            ids.append(saved.pk)
            created += 1
        if created:
            PlantEvent.objects.create(
                plant=plant,
                actor=actor,
                action="observations_imported",
                details={"configuration_id": configuration.pk, "created": created, "ids": ids},
            )
        return {"created": created, "existing": len(ids) - created, "count": len(ids), "ids": ids}


def request_calibration(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    *,
    configuration_id: int,
    start_date: date,
    end_date: date,
) -> CalibrationRun:
    require_member(actor, tenant_id)
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.select_for_update().get(pk=plant_id, region=region)
        configuration = _configuration(plant)
        now = timezone.now()
        if configuration_id != configuration.pk:
            raise ValidationError("Calibration requires the current configuration.")
        if (
            not 7 <= (end_date - start_date).days + 1 <= 90
            or end_date >= now.astimezone(ZoneInfo(configuration.binding.timezone)).date()
        ):
            raise ValidationError("Use 7 to 90 completed local days.")
        pending = CalibrationRun.objects.filter(
            plant=plant, status__in=("pending", "running")
        ).first()
        if pending is not None:
            if (pending.configuration_id, pending.window_start, pending.window_end) == (
                configuration.pk,
                start_date,
                end_date,
            ):
                return pending
            raise ValidationError("A calibration is already pending.")
        run = CalibrationRun.objects.create(
            plant=plant,
            configuration=configuration,
            binding=configuration.binding,
            requested_by=actor,
            window_start=start_date,
            window_end=end_date,
            cutoff_at=now,
        )
        PlantEvent.objects.create(
            plant=plant,
            actor=actor,
            action="calibration_requested",
            details={"calibration_id": run.pk},
        )
        return run


def _activate(
    actor: User, plant: Plant, configuration: PlantConfiguration, run: CalibrationRun | None
) -> CalibrationActivation:
    previous = (
        CalibrationActivation.objects.filter(plant=plant, configuration=configuration)
        .order_by("-created_at", "-pk")
        .first()
    )
    if previous is not None and previous.calibration_id == (run.pk if run else None):
        return previous
    activation = CalibrationActivation.objects.create(
        plant=plant, configuration=configuration, calibration=run, actor=actor
    )
    PlantEvent.objects.create(
        plant=plant,
        actor=actor,
        action="calibration_activated" if run else "calibration_reset",
        details={"activation_id": activation.pk, "calibration_id": run.pk if run else None},
    )
    return activation


def activate_calibration(
    actor: User, tenant_id: int, region_id: int, plant_id: int, calibration_id: int
) -> CalibrationActivation:
    require_member(actor, tenant_id)
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.select_for_update().get(pk=plant_id, region=region)
        configuration = _configuration(plant)
        run = CalibrationRun.objects.get(
            pk=calibration_id, plant=plant, configuration=configuration
        )
        now = timezone.now()
        if (
            run.binding_id != configuration.binding_id
            or run.status != "ready"
            or run.factor is None
            or not isfinite(run.factor)
            or run.factor <= 0
            or run.completed_at is None
            or run.completed_at > now
            or run.cutoff_at > now
            or run.assessment not in {"provisional", "evaluated"}
            or not run.result.get("holdout_days")
            or not run.result.get("candidate")
            or not run.result.get("baseline")
            or run.dataset_manifest.get("selection_policy") != HISTORY_SELECTION_POLICY
            or run.dataset_manifest.get("source_kind") != "estimated_actuals"
        ):
            raise ValidationError(
                "Only a completed, held-out evaluated calibration can be activated."
            )
        return _activate(actor, plant, configuration, run)


def reset_calibration(
    actor: User, tenant_id: int, region_id: int, plant_id: int
) -> CalibrationActivation:
    require_member(actor, tenant_id)
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.select_for_update().get(pk=plant_id, region=region)
        return _activate(actor, plant, _configuration(plant), None)
