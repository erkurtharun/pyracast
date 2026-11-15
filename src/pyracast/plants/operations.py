from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from typing import TypedDict
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext

from pyracast.accounts.operations import require_workspace_admin
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.actuals import ACTUAL_COMPOSITION_POLICY
from pyracast.forecasts.history import HISTORY_SELECTION_POLICY
from pyracast.forecasts.jobs import binding_is_current
from pyracast.plants.availability import interval_is_covered, reference_intervals
from pyracast.plants.measurement_policy import MEASUREMENT_POLICY
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
    PlantSubmission,
    ProductionObservation,
)
from pyracast.plants.numerics import POLICY_VERSION, PowerInterval
from pyracast.plants.preparation import prepare_calibration
from pyracast.plants.transfer import calibration_matches_transfer
from pyracast.regions.models import Region


@dataclass(frozen=True)
class ObservationInput:
    starts_at: datetime
    ends_at: datetime
    energy: float
    quality: str = "valid"
    source: str = "operator"


class ObservationImportResult(TypedDict):
    created: int
    existing: int
    count: int
    ids: list[int]


def configuration_is_current(configuration: PlantConfiguration) -> bool:
    return Plant.objects.filter(
        pk=configuration.plant_id,
        deleted_at__isnull=True,
        region__deleted_at__isnull=True,
        current_configuration_id=configuration.pk,
        region_id=configuration.binding.region_id,
    ).exists() and binding_is_current(configuration.binding)


def _binding(region: Region) -> SiteBinding:
    if region.approved_binding_id is None:
        raise ValidationError(gettext("An approved current provider binding is required."))
    binding = SiteBinding.objects.select_related("provider_account").get(
        pk=region.approved_binding_id, region=region
    )
    if not binding_is_current(binding):
        raise ValidationError(gettext("An approved current provider binding is required."))
    return binding


def _configuration(plant: Plant) -> PlantConfiguration:
    if plant.current_configuration_id is None:
        raise ValidationError("configuration_unavailable")
    configuration = PlantConfiguration.objects.select_related("binding__provider_account").get(
        pk=plant.current_configuration_id, plant=plant
    )
    if not configuration_is_current(configuration):
        raise ValidationError(gettext("The plant configuration is no longer current."))
    return configuration


def _validate_configuration(
    name: str,
    dc_capacity_mw: float,
    inverter_capacity_mw: float | None,
    export_limit_mw: float | None,
    measurement_basis: str,
    mounting_type: str,
    surface_tilt: float | None,
    surface_azimuth: float | None,
) -> None:
    if not name.strip() or len(name.strip()) > 200:
        raise ValidationError(gettext("A plant name of at most 200 characters is required."))
    for value in (dc_capacity_mw, inverter_capacity_mw, export_limit_mw):
        if value is not None and (isinstance(value, bool) or not isfinite(value) or value <= 0):
            raise ValidationError(gettext("Capacities must be finite and positive."))
    if measurement_basis not in {"net_export", "ac_generation"}:
        raise ValidationError(gettext("Unsupported meter basis."))
    if mounting_type not in {"unknown", "fixed", "tracking"}:
        raise ValidationError(gettext("Unsupported mounting type."))
    for angle, bound, exclusive in ((surface_tilt, 90, False), (surface_azimuth, 360, True)):
        if angle is not None and (
            isinstance(angle, bool)
            or not isfinite(angle)
            or angle < 0
            or angle > bound
            or (exclusive and angle == bound)
        ):
            raise ValidationError(
                gettext("Surface angles must be finite and inside the allowed bounds.")
            )
    if mounting_type != "fixed" and (surface_tilt is not None or surface_azimuth is not None):
        raise ValidationError(gettext("Only fixed mounting permits surface angles."))


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
    mounting_type: str,
    surface_tilt: float | None,
    surface_azimuth: float | None,
) -> None:
    revision = 1
    if plant.current_configuration_id is not None:
        revision = PlantConfiguration.objects.get(pk=plant.current_configuration_id).revision + 1
    configuration = PlantConfiguration.objects.create(
        plant=plant,
        region_id=plant.region_id,
        binding=binding,
        revision=revision,
        dc_capacity_mw=dc_capacity_mw,
        inverter_capacity_mw=inverter_capacity_mw,
        export_limit_mw=export_limit_mw,
        measurement_basis=measurement_basis,
        mounting_type=mounting_type,
        surface_tilt=surface_tilt,
        surface_azimuth=surface_azimuth,
        created_by=actor,
    )
    plant.name = name.strip()
    plant.current_configuration = configuration
    plant.automatic_calibration_enabled = False
    plant.automatic_calibration_enabled_by = None
    plant.next_calibration_check_at = None
    plant.save(
        update_fields=(
            "name",
            "current_configuration",
            "automatic_calibration_enabled",
            "automatic_calibration_enabled_by",
            "next_calibration_check_at",
        )
    )
    CalibrationRun.objects.filter(plant=plant, status__in=("pending", "running")).update(
        status="superseded", completed_at=timezone.now(), lease_token=None, lease_expires_at=None
    )
    PlantEvent.objects.create(
        plant=plant,
        actor=actor,
        action="configuration_created",
        details={"configuration_id": configuration.pk},
    )
    from pyracast.metering.models import EpiasConnection
    from pyracast.metering.scheduler import enqueue_connection

    connection = EpiasConnection.objects.filter(plant=plant, enabled=True).first()
    if connection is not None:
        enqueue_connection(connection, timezone.now(), initial=True)


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
    mounting_type: str = "unknown",
    surface_tilt: float | None = None,
    surface_azimuth: float | None = None,
) -> Plant:
    _validate_configuration(
        name,
        dc_capacity_mw,
        inverter_capacity_mw,
        export_limit_mw,
        measurement_basis,
        mounting_type,
        surface_tilt,
        surface_azimuth,
    )
    with transaction.atomic():
        require_workspace_admin(actor, tenant_id)
        region = Region.objects.select_for_update().get(
            pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True
        )
        if Plant.objects.filter(region=region, deleted_at__isnull=True).exists():
            raise ValidationError("location_already_has_plant")
        plant = Plant.objects.create(region=region, name=name.strip())
        _save_plant_parameters(
            actor,
            plant,
            region,
            name=name,
            dc_capacity_mw=dc_capacity_mw,
            inverter_capacity_mw=inverter_capacity_mw,
            export_limit_mw=export_limit_mw,
            measurement_basis=measurement_basis,
            mounting_type=mounting_type,
            surface_tilt=surface_tilt,
            surface_azimuth=surface_azimuth,
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
    mounting_type: str = "unknown",
    surface_tilt: float | None = None,
    surface_azimuth: float | None = None,
) -> Plant:
    _validate_configuration(
        name,
        dc_capacity_mw,
        inverter_capacity_mw,
        export_limit_mw,
        measurement_basis,
        mounting_type,
        surface_tilt,
        surface_azimuth,
    )
    with transaction.atomic():
        require_workspace_admin(actor, tenant_id)
        region = Region.objects.select_for_update().get(
            pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True
        )
        plant = Plant.objects.select_for_update().get(
            pk=plant_id, region=region, deleted_at__isnull=True
        )
        _save_plant_parameters(
            actor,
            plant,
            region,
            name=name,
            dc_capacity_mw=dc_capacity_mw,
            inverter_capacity_mw=inverter_capacity_mw,
            export_limit_mw=export_limit_mw,
            measurement_basis=measurement_basis,
            mounting_type=mounting_type,
            surface_tilt=surface_tilt,
            surface_azimuth=surface_azimuth,
        )
        return plant


def _save_plant_parameters(
    actor: User,
    plant: Plant,
    region: Region,
    *,
    name: str,
    dc_capacity_mw: float,
    inverter_capacity_mw: float | None,
    export_limit_mw: float | None,
    measurement_basis: str,
    mounting_type: str,
    surface_tilt: float | None,
    surface_azimuth: float | None,
) -> None:
    if region.status == "pending" and plant.current_configuration_id is None:
        PlantSubmission.objects.update_or_create(
            plant=plant,
            defaults={
                "dc_capacity_mw": dc_capacity_mw,
                "inverter_capacity_mw": inverter_capacity_mw,
                "export_limit_mw": export_limit_mw,
                "measurement_basis": measurement_basis,
                "mounting_type": mounting_type,
                "surface_tilt": surface_tilt,
                "surface_azimuth": surface_azimuth,
                "submitted_by": actor,
            },
        )
        plant.name = name.strip()
        plant.save(update_fields=("name",))
        PlantEvent.objects.create(plant=plant, actor=actor, action="submission_saved")
        return
    _save_configuration(
        actor,
        plant,
        _binding(region),
        name=name,
        dc_capacity_mw=dc_capacity_mw,
        inverter_capacity_mw=inverter_capacity_mw,
        export_limit_mw=export_limit_mw,
        measurement_basis=measurement_basis,
        mounting_type=mounting_type,
        surface_tilt=surface_tilt,
        surface_azimuth=surface_azimuth,
    )


def activate_submitted_plants(actor: User, region: Region, binding: SiteBinding) -> None:
    plants = Plant.objects.select_for_update().filter(region=region, deleted_at__isnull=True)
    for plant in plants:
        parameters: PlantConfiguration | PlantSubmission
        if plant.current_configuration_id is not None:
            parameters = PlantConfiguration.objects.get(pk=plant.current_configuration_id)
            if parameters.binding_id == binding.pk:
                continue
        else:
            submission = PlantSubmission.objects.filter(plant=plant).first()
            if submission is None:
                raise ValidationError("plant_submission_required")
            parameters = submission
        _save_configuration(
            actor,
            plant,
            binding,
            name=plant.name,
            dc_capacity_mw=parameters.dc_capacity_mw,
            inverter_capacity_mw=parameters.inverter_capacity_mw,
            export_limit_mw=parameters.export_limit_mw,
            measurement_basis=parameters.measurement_basis,
            mounting_type=parameters.mounting_type,
            surface_tilt=parameters.surface_tilt,
            surface_azimuth=parameters.surface_azimuth,
        )


def _observation(
    observation: ObservationInput, unit: str, zone: ZoneInfo, now: datetime
) -> tuple[datetime, datetime, float, str, str]:
    start, end = observation.starts_at, observation.ends_at
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValidationError(gettext("Observation timestamps must be timezone-aware."))
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end - start not in {timedelta(minutes=30), timedelta(minutes=60)} or end > now:
        raise ValidationError(gettext("Completed 30 or 60 minute observations are required."))
    local = start.astimezone(zone)
    for stamp in (local, end.astimezone(zone)):
        if stamp.minute not in (0, 30) or stamp.second or stamp.microsecond:
            raise ValidationError(gettext("Observations must align to local half-hour boundaries."))
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), zone)
    if end > midnight.astimezone(UTC):
        raise ValidationError(gettext("Observations must not cross local midnight."))
    value = observation.energy
    if isinstance(value, bool) or not isfinite(value) or value < 0:
        raise ValidationError(gettext("Observed energy must be finite and nonnegative."))
    if observation.quality not in {"valid", "outage", "curtailed", "invalid"}:
        raise ValidationError(gettext("Unsupported observation quality."))
    source = observation.source.strip()
    if not source or len(source) > 200:
        raise ValidationError(
            gettext("An observation source of at most 200 characters is required.")
        )
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
) -> ObservationImportResult:
    if unit not in {"MWh", "kWh"} or not 1 <= len(observations) <= 2000:
        raise ValidationError(gettext("Use MWh or kWh and a batch of 1 to 2000 observations."))
    with transaction.atomic():
        require_workspace_admin(actor, tenant_id)
        region = Region.objects.select_for_update().get(
            pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True
        )
        plant = Plant.objects.select_for_update().get(
            pk=plant_id, region=region, deleted_at__isnull=True
        )
        configuration = PlantConfiguration.objects.select_related("binding").get(
            pk=configuration_id, plant=plant
        )
        now, zone = timezone.now(), ZoneInfo(configuration.binding.timezone)
        created = 0
        ids: list[int] = []
        references: dict[date, tuple[PowerInterval, ...]] = {}
        for item in observations:
            start, end, value, quality, source = _observation(item, unit, zone, now)
            overlap = ProductionObservation.objects.filter(
                plant=plant, starts_at__lt=end, ends_at__gt=start, is_current=True
            ).first()
            if overlap is not None:
                if (
                    overlap.starts_at != start
                    or overlap.ends_at != end
                    or overlap.configuration_id != configuration.pk
                    or overlap.energy_mwh != value
                    or overlap.quality != quality
                    or overlap.source != source
                    or overlap.is_reverted
                ):
                    raise ValidationError(gettext("Conflicting or overlapping observation."))
                ids.append(overlap.pk)
                continue
            if not configuration_is_current(configuration):
                raise ValidationError(
                    gettext("Production entry requires the current approved configuration.")
                )
            day = start.astimezone(zone).date()
            if day not in references:
                references[day] = reference_intervals(configuration, day, now)
            if not interval_is_covered(references[day], start, end):
                raise ValidationError("estimated_actuals_unavailable")
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
            prepare_calibration(plant.pk)
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
    with transaction.atomic():
        require_workspace_admin(actor, tenant_id)
        region = Region.objects.select_for_update().get(
            pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True
        )
        plant = Plant.objects.select_for_update().get(
            pk=plant_id, region=region, deleted_at__isnull=True
        )
        configuration = _configuration(plant)
        now = timezone.now()
        if configuration_id != configuration.pk:
            raise ValidationError(gettext("Calibration requires the current configuration."))
        if (
            not 7 <= (end_date - start_date).days + 1 <= 90
            or end_date >= now.astimezone(ZoneInfo(configuration.binding.timezone)).date()
        ):
            raise ValidationError(gettext("Use 7 to 90 completed local days."))
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
            raise ValidationError(gettext("A calibration is already pending."))
        comparison = (
            CalibrationActivation.objects.filter(plant=plant, configuration=configuration)
            .order_by("-created_at", "-pk")
            .first()
        )
        from pyracast.irradiance import local_day_bounds
        from pyracast.plants.measurements import select_measurements, selection_fingerprint

        start, _ = local_day_bounds(start_date, configuration.binding.timezone)
        _, end = local_day_bounds(end_date, configuration.binding.timezone)
        watermark = (
            ProductionObservation.objects.filter(
                configuration=configuration, recorded_at__lte=now
            ).aggregate(last=Max("pk"))["last"]
            or 0
        )
        rows, snapshot_ids = select_measurements(
            configuration, start, end, now, observation_id_cutoff=watermark
        )
        from pyracast.plants.incident_policy import incident_ids

        incidents = incident_ids(configuration, start, end, now)
        fingerprint, reference_ids = selection_fingerprint(configuration, rows, now, incidents)
        run = CalibrationRun.objects.create(
            plant=plant,
            configuration=configuration,
            binding=configuration.binding,
            comparison_activation=comparison,
            requested_by=actor,
            window_start=start_date,
            window_end=end_date,
            cutoff_at=now,
            observation_id_cutoff=watermark,
            production_snapshot_ids=snapshot_ids,
            measurement_selection_policy=MEASUREMENT_POLICY,
            reference_snapshot_ids=reference_ids,
            incident_revision_ids=incidents,
            input_fingerprint=fingerprint,
        )
        PlantEvent.objects.create(
            plant=plant,
            actor=actor,
            action="calibration_requested",
            details={"calibration_id": run.pk},
        )
        return run


def _activate(
    actor: User | None, plant: Plant, configuration: PlantConfiguration, run: CalibrationRun | None
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
        action=(
            "calibration_automatically_activated"
            if actor is None
            else "calibration_activated"
            if run
            else "calibration_reset"
        ),
        details={
            "activation_id": activation.pk,
            "calibration_id": run.pk if run else None,
            "configuration_id": configuration.pk,
            "origin": "automatic" if actor is None else "manual",
            "enabled_by_id": plant.automatic_calibration_enabled_by_id if actor is None else None,
        },
    )
    return activation


def _validate_activation(
    plant: Plant, configuration: PlantConfiguration, run: CalibrationRun, now: datetime
) -> None:
    from pyracast.plants.incident_policy import incidents_unchanged
    from pyracast.plants.measurements import production_identity_current

    if not production_identity_current(run):
        raise ValidationError(gettext("The production source changed; request a new calibration."))
    if not incidents_unchanged(run, now):
        raise ValidationError(gettext("The outage history changed; request a new calibration."))
    current = (
        CalibrationActivation.objects.filter(plant=plant, configuration=configuration)
        .order_by("-created_at", "-pk")
        .first()
    )
    if current is not None and current.calibration_id == run.pk:
        return
    if (current.pk if current else None) != run.comparison_activation_id:
        raise ValidationError(gettext("The active calibration changed; request a new comparison."))
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
        or run.result.get("policy_version") != POLICY_VERSION
        or not calibration_matches_transfer(run, configuration)
        or run.dataset_manifest.get("selection_policy")
        not in (HISTORY_SELECTION_POLICY, ACTUAL_COMPOSITION_POLICY)
        or run.dataset_manifest.get("source_kind") != "estimated_actuals"
    ):
        raise ValidationError(
            gettext("Only a completed, held-out evaluated calibration can be activated.")
        )


def activate_calibration(
    actor: User, tenant_id: int, region_id: int, plant_id: int, calibration_id: int
) -> CalibrationActivation:
    with transaction.atomic():
        require_workspace_admin(actor, tenant_id)
        region = Region.objects.select_for_update().get(
            pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True
        )
        plant = Plant.objects.select_for_update().get(
            pk=plant_id, region=region, deleted_at__isnull=True
        )
        configuration = _configuration(plant)
        run = CalibrationRun.objects.get(
            pk=calibration_id, plant=plant, configuration=configuration
        )
        now = timezone.now()
        _validate_activation(plant, configuration, run, now)
        from pyracast.plants.automation import update_automation

        update_automation(plant, actor, enabled=True, now=now)
        return _activate(actor, plant, configuration, run)


def reset_calibration(
    actor: User, tenant_id: int, region_id: int, plant_id: int
) -> CalibrationActivation:
    with transaction.atomic():
        require_workspace_admin(actor, tenant_id)
        region = Region.objects.select_for_update().get(
            pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True
        )
        plant = Plant.objects.select_for_update().get(
            pk=plant_id, region=region, deleted_at__isnull=True
        )
        from pyracast.plants.automation import update_automation

        update_automation(plant, actor, enabled=False, now=timezone.now())
        return _activate(actor, plant, _configuration(plant), None)
