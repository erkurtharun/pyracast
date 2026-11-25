from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy

from pyracast.accounts.authorization import authorize_workspace_admin, tenant_membership
from pyracast.accounts.models import WorkspaceRole
from pyracast.connections.currency import binding_is_current
from pyracast.connections.models import SiteBinding
from pyracast.errors import TECHNICAL_DETAILS_CHANGED, ConflictError, DomainError, ErrorCode
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
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEventAction,
    PlantSubmission,
    ProductionObservation,
)
from pyracast.plants.parameters import PlantParameters, declaration, declared_parameters
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction, Region, RegionStatus

if TYPE_CHECKING:
    from django_stubs_ext import StrPromise


class StaleParametersError(ConflictError):
    def __init__(self, current: Plant) -> None:
        super().__init__(ErrorCode.PLANT_PARAMETERS_CHANGED)
        self.current = current


def configuration_is_current(configuration: PlantConfiguration) -> bool:
    return Plant.objects.active().filter(
        pk=configuration.plant_id,
        current_configuration_id=configuration.pk,
        region_id=configuration.binding.region_id,
    ).exists() and binding_is_current(configuration.binding)


class PlantEditBlock(models.TextChoices):
    SUSPENDED = "suspended"
    CONNECTION_UNAVAILABLE = "connection_unavailable"


EDIT_BLOCK_MESSAGES: Mapping[PlantEditBlock, StrPromise] = {
    PlantEditBlock.SUSPENDED: gettext_lazy(
        "Pyracast suspended this plant, so its technical details cannot be changed."
    ),
    PlantEditBlock.CONNECTION_UNAVAILABLE: gettext_lazy(
        "No new forecast data is arriving for this plant, "
        "so its technical details cannot be changed yet."
    ),
}


def plant_edit_block(region: Region) -> PlantEditBlock | None:
    """Why a plant's declared values cannot change now; reads the region's approved binding
    and its provider account, so a list loads them with the region."""
    if region.status == RegionStatus.SUSPENDED:
        return PlantEditBlock.SUSPENDED
    binding = region.approved_binding
    if region.status == RegionStatus.APPROVED and (
        binding is None or not binding_is_current(binding, region)
    ):
        return PlantEditBlock.CONNECTION_UNAVAILABLE
    return None


def _editable_binding(region: Region) -> SiteBinding | None:
    """The approved binding a change is saved against, or None while the plant is in review."""
    block = plant_edit_block(region)
    if block is not None:
        raise DomainError(ErrorCode.PLANT_EDIT_BLOCKED, EDIT_BLOCK_MESSAGES[block])
    return region.approved_binding


def current_configuration(plant: Plant) -> PlantConfiguration:
    if plant.current_configuration_id is None:
        raise DomainError(ErrorCode.CONFIGURATION_UNAVAILABLE)
    configuration = PlantConfiguration.objects.select_related("binding__provider_account").get(
        pk=plant.current_configuration_id, plant=plant
    )
    if not configuration_is_current(configuration):
        raise DomainError(ErrorCode.CONFIGURATION_CHANGED)
    return configuration


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


def _locked_plant(**lookup: object) -> Plant:
    return (
        Plant.objects.select_for_update(of=("self",))
        .select_related("submission", "current_configuration")
        .get(**lookup)
    )


def create_plant(
    actor: User,
    tenant_id: int,
    region_id: int,
    parameters: PlantParameters,
    *,
    platform: bool = False,
) -> Plant:
    with transaction.atomic():
        authorize_workspace_admin(actor, tenant_id, platform=platform)
        region = Region.objects.active().select_for_update().get(pk=region_id, tenant_id=tenant_id)
        if Plant.objects.filter(region=region).exists():
            raise DomainError(ErrorCode.LOCATION_ALREADY_HAS_PLANT)
        plant = Plant.objects.create(region=region, name=parameters.name)
        _save_plant_parameters(actor, plant, region, parameters, expected_revision=None)
        return plant


def revise_plant(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    parameters: PlantParameters,
    *,
    expected_revision: int | None = None,
    platform: bool = False,
) -> Plant:
    """`expected_revision` is the parameter revision the change was made from; a different
    change saved since then refuses it. None overwrites, as the operator command does unless
    it is given a revision."""
    return revise_plant_outcome(
        actor,
        tenant_id,
        region_id,
        plant_id,
        parameters,
        expected_revision=expected_revision,
        platform=platform,
    ).plant


@dataclass(frozen=True)
class PlantRevision:
    """`before` is the parameters revision the save started from under the plant's lock."""

    plant: Plant
    before: int
    changed: bool


def revise_plant_outcome(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    parameters: PlantParameters,
    *,
    expected_revision: int | None = None,
    platform: bool = False,
) -> PlantRevision:
    with transaction.atomic():
        authorize_workspace_admin(actor, tenant_id, platform=platform)
        region = Region.objects.active().select_for_update().get(pk=region_id, tenant_id=tenant_id)
        plant = _locked_plant(pk=plant_id, region=region)
        before = plant.parameters_revision
        changed = _save_plant_parameters(actor, plant, region, parameters, expected_revision)
        return PlantRevision(plant, before, changed)


def _save_plant_parameters(
    actor: User,
    plant: Plant,
    region: Region,
    parameters: PlantParameters,
    expected_revision: int | None,
) -> bool:
    """Returns whether the save wrote anything."""
    binding = _editable_binding(region)
    current = declared_parameters(plant)
    configuration = plant.current_configuration
    rebound = binding is not None and (
        configuration is None or configuration.binding_id != binding.pk
    )
    if current == parameters and not rebound:
        return False
    if current != parameters and expected_revision not in (None, plant.parameters_revision):
        raise StaleParametersError(plant)
    _rename(actor, plant, region, parameters.name)
    if current is None or current.declared() != parameters.declared() or rebound:
        if binding is None:
            _submit(actor, plant, parameters)
        else:
            _save_configuration(actor, plant, {"binding": binding, **parameters.declared()})
    if current is not None and current != parameters:
        plant.parameters_revision += 1
        plant.save(update_fields=("parameters_revision",))
    return True


def _submit(actor: User, plant: Plant, parameters: PlantParameters) -> None:
    """Records the declaration for the plant's review; approval activates it."""
    plant.submission, _ = PlantSubmission.objects.update_or_create(
        plant=plant,
        defaults={
            **parameters.declared(),
            "submitted_by": actor,
            "base_configuration_id": plant.current_configuration_id,
        },
    )
    record_plant_event(plant_id=plant.pk, actor=actor, action=PlantEventAction.SUBMISSION_SAVED)


def activate_submitted_plants(actor: User, region: Region, binding: SiteBinding) -> None:
    for plant in (
        Plant.objects.select_for_update(of=("self",))
        .select_related("submission", "current_configuration")
        .filter(region=region)
    ):
        record = declaration(plant)
        if record is None:
            raise DomainError(ErrorCode.PLANT_SUBMISSION_REQUIRED)
        if isinstance(record, PlantConfiguration) and record.binding_id == binding.pk:
            continue
        parameters = PlantParameters.of(plant.name, record)
        _save_configuration(actor, plant, {"binding": binding, **parameters.declared()})


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
            raise ValidationError(TECHNICAL_DETAILS_CHANGED)
        if (
            not 7 <= (end_date - start_date).days + 1 <= 90
            or end_date >= now.astimezone(ZoneInfo(configuration.binding.timezone)).date()
        ):
            raise ValidationError(
                gettext("Choose a period of 7 to 90 days that ends before today.")
            )
        pending = CalibrationRun.objects.filter(plant=plant, status__in=OPEN_STATUSES).first()
        if pending is not None:
            if (pending.configuration_id, pending.window_start, pending.window_end) == (
                configuration.pk,
                start_date,
                end_date,
            ):
                return pending
            raise ValidationError(gettext("A calibration is already queued or being calculated."))
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
