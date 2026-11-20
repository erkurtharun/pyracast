from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any

from django.contrib.auth.models import User
from django.db import models, transaction
from django.utils import timezone

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import WorkspaceRole
from pyracast.errors import DomainError, ErrorCode
from pyracast.forecasts.actuals import ACTUAL_COMPOSITION_POLICY
from pyracast.forecasts.history import HISTORY_SELECTION_POLICY
from pyracast.forecasts.models import SnapshotSourceKind
from pyracast.plants.automation_state import AutomationCause, set_automation_state
from pyracast.plants.calibration_runs import current_activation
from pyracast.plants.events import record_plant_event
from pyracast.plants.forecasting import calibration_at
from pyracast.plants.incident_policy import incidents_unchanged
from pyracast.plants.measurements import production_identity_current
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationAssessment,
    CalibrationRun,
    CalibrationStatus,
    Plant,
    PlantConfiguration,
    PlantEventAction,
)
from pyracast.plants.numerics import POLICY_VERSION
from pyracast.plants.operations import configuration_is_current, current_configuration
from pyracast.plants.transfer import calibration_matches_transfer
from pyracast.regions.models import Region


class ActivationBlocker(models.TextChoices):
    """Why a run cannot be activated; each value is also the API error code of the refusal."""

    CONFIGURATION_CHANGED = ErrorCode.CONFIGURATION_CHANGED.value
    PRODUCTION_SOURCE_CHANGED = ErrorCode.PRODUCTION_SOURCE_CHANGED.value
    OUTAGE_HISTORY_CHANGED = ErrorCode.OUTAGE_HISTORY_CHANGED.value
    INCUMBENT_CHANGED = ErrorCode.INCUMBENT_CHANGED.value
    CALIBRATION_NOT_ACTIVATABLE = ErrorCode.CALIBRATION_NOT_ACTIVATABLE.value


class SelectionState(models.TextChoices):
    ACTIVE = "active"
    CANDIDATE = "candidate"
    PREVIOUS_CONFIGURATION = "previous_configuration"
    NOT_READY = "not_ready"


@dataclass(frozen=True)
class Selection:
    configuration_id: int | None
    active_calibration_id: int | None


def _activatable(run: CalibrationRun, configuration: PlantConfiguration, now: datetime) -> bool:
    return (
        run.binding_id == configuration.binding_id
        and run.status == CalibrationStatus.READY
        and run.factor is not None
        and isfinite(run.factor)
        and run.factor > 0
        and run.completed_at is not None
        and run.completed_at <= now
        and run.cutoff_at <= now
        and run.assessment in {CalibrationAssessment.PROVISIONAL, CalibrationAssessment.EVALUATED}
        and bool(run.result.get("holdout_days"))
        and bool(run.result.get("candidate"))
        and bool(run.result.get("baseline"))
        and run.result.get("policy_version") == POLICY_VERSION
        and calibration_matches_transfer(run, configuration)
        and run.dataset_manifest.get("selection_policy")
        in (HISTORY_SELECTION_POLICY, ACTUAL_COMPOSITION_POLICY)
        and run.dataset_manifest.get("source_kind") == SnapshotSourceKind.ESTIMATED_ACTUALS
    )


def activation_blocker(
    plant: Plant, configuration: PlantConfiguration | None, run: CalibrationRun, now: datetime
) -> ActivationBlocker | None:
    """The one activation policy: manual activation, automatic promotion and the run read
    model all ask it, so a screen shows the reason the operation refuses with."""
    if (
        configuration is None
        or run.configuration_id != configuration.pk
        or not configuration_is_current(configuration)
    ):
        return ActivationBlocker.CONFIGURATION_CHANGED
    if not production_identity_current(run):
        return ActivationBlocker.PRODUCTION_SOURCE_CHANGED
    if not incidents_unchanged(run, now):
        return ActivationBlocker.OUTAGE_HISTORY_CHANGED
    current = current_activation(plant, configuration)
    if current is not None and current.calibration_id == run.pk:
        return None
    if (current.pk if current else None) != run.comparison_activation_id:
        return ActivationBlocker.INCUMBENT_CHANGED
    if not _activatable(run, configuration, now):
        return ActivationBlocker.CALIBRATION_NOT_ACTIVATABLE
    return None


def record_activation(
    actor: User | None, plant: Plant, configuration: PlantConfiguration, run: CalibrationRun | None
) -> CalibrationActivation:
    previous = current_activation(plant, configuration)
    if previous is not None and previous.calibration_id == (run.pk if run else None):
        return previous
    activation = CalibrationActivation.objects.create(
        plant=plant, configuration=configuration, calibration=run, actor=actor
    )
    record_plant_event(
        plant_id=plant.pk,
        actor=actor,
        action=(
            PlantEventAction.CALIBRATION_AUTOMATICALLY_ACTIVATED
            if actor is None
            else PlantEventAction.CALIBRATION_ACTIVATED
            if run
            else PlantEventAction.CALIBRATION_RESET
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


def activate_calibration(
    actor: User, tenant_id: int, region_id: int, plant_id: int, calibration_id: int
) -> CalibrationActivation:
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        region = Region.objects.active().select_for_update().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.select_for_update().get(pk=plant_id, region=region)
        configuration = current_configuration(plant)
        run = CalibrationRun.objects.get(pk=calibration_id, plant=plant)
        now = timezone.now()
        blocker = activation_blocker(plant, configuration, run, now)
        if blocker is not None:
            raise DomainError(ErrorCode(blocker))
        set_automation_state(
            plant, actor, enabled=True, cause=AutomationCause.MANUAL_ACTIVATION, now=now
        )
        return record_activation(actor, plant, configuration, run)


def reset_calibration(
    actor: User, tenant_id: int, region_id: int, plant_id: int
) -> CalibrationActivation:
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        region = Region.objects.active().select_for_update().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.select_for_update().get(pk=plant_id, region=region)
        set_automation_state(
            plant, actor, enabled=False, cause=AutomationCause.RESET, now=timezone.now()
        )
        return record_activation(actor, plant, current_configuration(plant), None)


def selection(plant: Plant) -> Selection:
    configuration = plant.current_configuration
    current = None if configuration is None else current_activation(plant, configuration)
    return Selection(
        plant.current_configuration_id, current.calibration_id if current is not None else None
    )


def selection_state(run: CalibrationRun, current: Selection) -> SelectionState:
    if run.configuration_id != current.configuration_id:
        return SelectionState.PREVIOUS_CONFIGURATION
    if run.pk == current.active_calibration_id:
        return SelectionState.ACTIVE
    if run.status == CalibrationStatus.READY:
        return SelectionState.CANDIDATE
    return SelectionState.NOT_READY


def current_selection(plant: Plant, now: datetime) -> dict[str, Any] | None:
    configuration = plant.current_configuration
    current = None if configuration is None else current_activation(plant, configuration)
    if configuration is None or current is None:
        return None
    run = current.calibration
    applied = calibration_at(configuration, now)
    return {
        "activation_id": current.pk,
        "calibration_id": current.calibration_id,
        "factor": run.factor if run is not None else None,
        "actor_id": current.actor_id,
        "origin": "manual" if current.actor_id is not None else "automatic",
        "created_at": current.created_at,
        "effective": run is None or (applied is not None and applied.pk == run.pk),
    }
