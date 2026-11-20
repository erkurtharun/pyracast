from datetime import date
from math import isfinite
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext

from pyracast.accounts.authorization import authorize_workspace_admin, tenant_membership
from pyracast.accounts.models import WorkspaceRole
from pyracast.connections.currency import binding_is_current
from pyracast.connections.models import SiteBinding
from pyracast.errors import DomainError, ErrorCode
from pyracast.plants.automation_state import AutomationCause, set_automation_state
from pyracast.plants.calibration_runs import (
    OPEN_STATUSES,
    SupersedeReason,
    create_run,
    current_activation,
    pin_inputs,
    supersede_open_runs,
)
from pyracast.plants.events import record_plant_event
from pyracast.plants.models import (
    CONFIGURATION_PARAMETERS,
    CalibrationRun,
    MeasurementBasis,
    MountingType,
    Plant,
    PlantConfiguration,
    PlantEventAction,
    PlantSubmission,
    ProductionObservation,
)
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction, Region, RegionStatus


def configuration_is_current(configuration: PlantConfiguration) -> bool:
    return Plant.objects.active().filter(
        pk=configuration.plant_id,
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


def current_configuration(plant: Plant) -> PlantConfiguration:
    if plant.current_configuration_id is None:
        raise DomainError(ErrorCode.CONFIGURATION_UNAVAILABLE)
    configuration = PlantConfiguration.objects.select_related("binding__provider_account").get(
        pk=plant.current_configuration_id, plant=plant
    )
    if not configuration_is_current(configuration):
        raise DomainError(ErrorCode.CONFIGURATION_CHANGED)
    return configuration


def _validate_configuration(
    name: str,
    dc_capacity_mw: float,
    inverter_capacity_mw: float | None,
    export_limit_mw: float | None,
    measurement_basis: str,
    mounting_type: str,
    surface_tilt_degrees: float | None,
    surface_azimuth_degrees: float | None,
) -> None:
    if not name.strip() or len(name.strip()) > 200:
        raise ValidationError(gettext("A plant name of at most 200 characters is required."))
    for value in (dc_capacity_mw, inverter_capacity_mw, export_limit_mw):
        if value is not None and (isinstance(value, bool) or not isfinite(value) or value <= 0):
            raise ValidationError(gettext("Capacities must be finite and positive."))
    if measurement_basis not in MeasurementBasis.values:
        raise ValidationError(gettext("Unsupported meter basis."))
    if mounting_type not in MountingType.values:
        raise ValidationError(gettext("Unsupported mounting type."))
    for angle, bound, exclusive in (
        (surface_tilt_degrees, 90, False),
        (surface_azimuth_degrees, 360, True),
    ):
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
    if mounting_type != MountingType.FIXED and (
        surface_tilt_degrees is not None or surface_azimuth_degrees is not None
    ):
        raise ValidationError(gettext("Only fixed mounting permits surface angles."))


def _save_configuration(actor: User, plant: Plant, values: dict[str, object]) -> None:
    revision = 1
    if plant.current_configuration_id is not None:
        revision = PlantConfiguration.objects.get(pk=plant.current_configuration_id).revision + 1
    configuration = PlantConfiguration.objects.create(
        plant=plant, region_id=plant.region_id, revision=revision, created_by=actor, **values
    )
    plant.current_configuration = configuration
    plant.save(update_fields=("current_configuration",))
    now = timezone.now()
    set_automation_state(
        plant, actor, enabled=False, cause=AutomationCause.CONFIGURATION_REVISED, now=now
    )
    supersede_open_runs([plant.pk], SupersedeReason.CONFIGURATION_CHANGED, now)
    record_plant_event(
        plant_id=plant.pk,
        actor=actor,
        action=PlantEventAction.CONFIGURATION_CREATED,
        details={"configuration_id": configuration.pk},
    )
    from pyracast.metering.models import EpiasConnection
    from pyracast.metering.scheduler import enqueue_connection

    connection = EpiasConnection.objects.filter(plant=plant, enabled=True).first()
    if connection is not None:
        enqueue_connection(connection, timezone.now(), initial=True)


def _rename(actor: User, plant: Plant, region: Region, name: str) -> None:
    name = name.strip()
    if plant.name == name:
        return
    record_plant_event(
        plant_id=plant.pk,
        actor=actor,
        action=PlantEventAction.PLANT_RENAMED,
        details={"old": plant.name, "new": name},
    )
    plant.name = name
    plant.save(update_fields=("name",))
    if region.name != name:
        region.name = name
        region.save(update_fields=("name",))
        record_audit(
            actor=actor, region=region, action=AuditAction.REGION_RENAMED, revision=region.revision
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
    measurement_basis: str = MeasurementBasis.NET_EXPORT,
    mounting_type: str = MountingType.UNKNOWN,
    surface_tilt_degrees: float | None = None,
    surface_azimuth_degrees: float | None = None,
    platform: bool = False,
) -> Plant:
    _validate_configuration(
        name,
        dc_capacity_mw,
        inverter_capacity_mw,
        export_limit_mw,
        measurement_basis,
        mounting_type,
        surface_tilt_degrees,
        surface_azimuth_degrees,
    )
    with transaction.atomic():
        authorize_workspace_admin(actor, tenant_id, platform=platform)
        region = Region.objects.active().select_for_update().get(pk=region_id, tenant_id=tenant_id)
        if Plant.objects.filter(region=region).exists():
            raise DomainError(ErrorCode.LOCATION_ALREADY_HAS_PLANT)
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
            surface_tilt_degrees=surface_tilt_degrees,
            surface_azimuth_degrees=surface_azimuth_degrees,
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
    measurement_basis: str = MeasurementBasis.NET_EXPORT,
    mounting_type: str = MountingType.UNKNOWN,
    surface_tilt_degrees: float | None = None,
    surface_azimuth_degrees: float | None = None,
    platform: bool = False,
) -> Plant:
    _validate_configuration(
        name,
        dc_capacity_mw,
        inverter_capacity_mw,
        export_limit_mw,
        measurement_basis,
        mounting_type,
        surface_tilt_degrees,
        surface_azimuth_degrees,
    )
    with transaction.atomic():
        authorize_workspace_admin(actor, tenant_id, platform=platform)
        region = Region.objects.active().select_for_update().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.select_for_update().get(pk=plant_id, region=region)
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
            surface_tilt_degrees=surface_tilt_degrees,
            surface_azimuth_degrees=surface_azimuth_degrees,
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
    surface_tilt_degrees: float | None,
    surface_azimuth_degrees: float | None,
) -> None:
    parameters: dict[str, object] = {
        "dc_capacity_mw": dc_capacity_mw,
        "inverter_capacity_mw": inverter_capacity_mw,
        "export_limit_mw": export_limit_mw,
        "measurement_basis": measurement_basis,
        "mounting_type": mounting_type,
        "surface_tilt_degrees": surface_tilt_degrees,
        "surface_azimuth_degrees": surface_azimuth_degrees,
    }
    if region.status == RegionStatus.PENDING and plant.current_configuration_id is None:
        _rename(actor, plant, region, name)
        if PlantSubmission.objects.filter(plant=plant, **parameters).exists():
            return
        PlantSubmission.objects.update_or_create(
            plant=plant, defaults={**parameters, "submitted_by": actor}
        )
        record_plant_event(plant_id=plant.pk, actor=actor, action=PlantEventAction.SUBMISSION_SAVED)
        return
    values = {"binding": _binding(region), **parameters}
    _rename(actor, plant, region, name)
    current_id = plant.current_configuration_id
    if (
        current_id is not None
        and PlantConfiguration.objects.filter(pk=current_id, **values).exists()
    ):
        return
    _save_configuration(actor, plant, values)


def activate_submitted_plants(actor: User, region: Region, binding: SiteBinding) -> None:
    plants = Plant.objects.select_for_update().filter(region=region)
    for plant in plants:
        parameters: PlantConfiguration | PlantSubmission
        if plant.current_configuration_id is not None:
            parameters = PlantConfiguration.objects.get(pk=plant.current_configuration_id)
            if parameters.binding_id == binding.pk:
                continue
        else:
            submission = PlantSubmission.objects.filter(plant=plant).first()
            if submission is None:
                raise DomainError(ErrorCode.PLANT_SUBMISSION_REQUIRED)
            parameters = submission
        _save_configuration(
            actor,
            plant,
            {
                "binding": binding,
                **{name: getattr(parameters, name) for name in CONFIGURATION_PARAMETERS},
            },
        )


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
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        region = Region.objects.active().select_for_update().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.select_for_update().get(pk=plant_id, region=region)
        configuration = current_configuration(plant)
        now = timezone.now()
        if configuration_id != configuration.pk:
            raise ValidationError(gettext("Calibration requires the current configuration."))
        if (
            not 7 <= (end_date - start_date).days + 1 <= 90
            or end_date >= now.astimezone(ZoneInfo(configuration.binding.timezone)).date()
        ):
            raise ValidationError(gettext("Use 7 to 90 completed local days."))
        pending = CalibrationRun.objects.filter(plant=plant, status__in=OPEN_STATUSES).first()
        if pending is not None:
            if (pending.configuration_id, pending.window_start, pending.window_end) == (
                configuration.pk,
                start_date,
                end_date,
            ):
                return pending
            raise ValidationError(gettext("A calibration is already pending."))
        watermark = (
            ProductionObservation.objects.filter(
                configuration=configuration, created_at__lte=now
            ).aggregate(last=Max("pk"))["last"]
            or 0
        )
        return create_run(
            plant,
            configuration,
            pin_inputs(configuration, start_date, end_date, now, watermark),
            comparison=current_activation(plant, configuration),
            requested_by=actor,
            manual=True,
        )
