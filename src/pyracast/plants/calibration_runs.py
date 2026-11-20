import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.contrib.auth.models import User
from django.db import models

from pyracast.irradiance import local_day_bounds
from pyracast.plants.events import record_plant_event
from pyracast.plants.incident_policy import incident_ids
from pyracast.plants.measurement_policy import MEASUREMENT_POLICY
from pyracast.plants.measurements import select_measurements, selection_fingerprint
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    CalibrationStatus,
    Plant,
    PlantConfiguration,
    PlantEventAction,
)

OPEN_STATUSES = (CalibrationStatus.PENDING, CalibrationStatus.RUNNING)


class SupersedeReason(models.TextChoices):
    CONFIGURATION_CHANGED = "configuration_changed"
    BINDING_CHANGED = "binding_changed"
    PRODUCTION_SOURCE_CHANGED = "production_source_changed"
    OUTAGE_HISTORY_CHANGED = "outage_history_changed"
    TRASHED = "trashed"


@dataclass(frozen=True)
class RunInputs:
    window_start: date
    window_end: date
    cutoff_at: datetime
    observation_id_cutoff: int
    production_snapshot_ids: list[int]
    reference_snapshot_ids: dict[str, object]
    incident_revision_ids: list[int]
    input_sha256: str


def current_activation(
    plant: Plant, configuration: PlantConfiguration
) -> CalibrationActivation | None:
    return (
        CalibrationActivation.objects.filter(plant=plant, configuration=configuration)
        .order_by("-created_at", "-pk")
        .first()
    )


def pin_inputs(
    configuration: PlantConfiguration, first: date, last: date, cutoff: datetime, watermark: int
) -> RunInputs:
    start, _ = local_day_bounds(first, configuration.binding.timezone)
    _, end = local_day_bounds(last, configuration.binding.timezone)
    rows, snapshot_ids = select_measurements(
        configuration, start, end, cutoff, observation_id_cutoff=watermark
    )
    incidents = incident_ids(configuration, start, end, cutoff)
    fingerprint, references = selection_fingerprint(configuration, rows, cutoff, incidents)
    return RunInputs(
        window_start=first,
        window_end=last,
        cutoff_at=cutoff,
        observation_id_cutoff=watermark,
        production_snapshot_ids=snapshot_ids,
        reference_snapshot_ids=references,
        incident_revision_ids=incidents,
        input_sha256=fingerprint,
    )


def create_run(
    plant: Plant,
    configuration: PlantConfiguration,
    inputs: RunInputs,
    *,
    comparison: CalibrationActivation | None,
    requested_by: User,
    manual: bool,
) -> CalibrationRun:
    run = CalibrationRun.objects.create(
        plant=plant,
        configuration=configuration,
        binding=configuration.binding,
        comparison_activation=comparison,
        requested_by=requested_by,
        measurement_selection_policy=MEASUREMENT_POLICY,
        window_start=inputs.window_start,
        window_end=inputs.window_end,
        cutoff_at=inputs.cutoff_at,
        observation_id_cutoff=inputs.observation_id_cutoff,
        production_snapshot_ids=inputs.production_snapshot_ids,
        reference_snapshot_ids=inputs.reference_snapshot_ids,
        incident_revision_ids=inputs.incident_revision_ids,
        input_sha256=inputs.input_sha256,
    )
    record_plant_event(
        plant_id=plant.pk,
        actor=requested_by if manual else None,
        action=PlantEventAction.CALIBRATION_REQUESTED
        if manual
        else PlantEventAction.CALIBRATION_SCHEDULED,
        details={"calibration_id": run.pk}
        if manual
        else {
            "calibration_id": run.pk,
            "observation_id_cutoff": inputs.observation_id_cutoff,
            "origin": "automatic",
        },
    )
    return run


def start_run(run: CalibrationRun, now: datetime, lease: timedelta) -> uuid.UUID:
    token = uuid.uuid4()
    run.status = CalibrationStatus.RUNNING
    run.attempts += 1
    run.lease_token = token
    run.lease_expires_at = now + lease
    run.save(update_fields=("status", "attempts", "lease_token", "lease_expires_at"))
    return token


def requeue_run(run: CalibrationRun, reason: str) -> None:
    run.status = CalibrationStatus.PENDING
    run.last_error = reason
    run.lease_token = None
    run.lease_expires_at = None
    run.save(update_fields=("status", "last_error", "lease_token", "lease_expires_at"))


def finish_run(run: CalibrationRun, status: str, now: datetime, reason: str = "") -> None:
    """End a run: the caller holds the plant lock and has set the result fields."""
    run.status = status
    run.completed_at = now
    run.lease_token = None
    run.lease_expires_at = None
    run.last_error = reason if status == CalibrationStatus.FAILED else ""
    run.save()
    record_plant_event(
        plant_id=run.plant_id,
        actor=None,
        action=PlantEventAction.CALIBRATION_COMPLETED,
        details={
            "calibration_id": run.pk,
            "status": status,
            "reason": reason or None,
            "dataset_sha256": run.dataset_sha256,
        },
    )


def supersede_open_runs(plant_ids: Iterable[int], reason: str, now: datetime) -> None:
    for run in CalibrationRun.objects.filter(
        plant_id__in=list(plant_ids), status__in=OPEN_STATUSES
    ).order_by("pk"):
        finish_run(run, CalibrationStatus.SUPERSEDED, now, reason)
