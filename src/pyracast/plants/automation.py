import logging
from datetime import date, datetime
from functools import partial
from math import isfinite
from typing import Any, cast
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from pyracast.accounts.authorization import require_platform_admin, tenant_membership
from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.background.failures import isolate, log_failure
from pyracast.plants.activation import (
    activation_blocker,
    current_selection,
    record_activation,
    selection,
)
from pyracast.plants.automation_state import AutomationCause, next_check, set_automation_state
from pyracast.plants.calibration_data import current_source_validation
from pyracast.plants.calibration_runs import OPEN_STATUSES
from pyracast.plants.events import record_plant_event
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationAssessment,
    CalibrationRun,
    CalibrationStatus,
    Plant,
    PlantEvent,
    PlantEventAction,
    PlantQuerySet,
)
from pyracast.plants.operations import configuration_is_current, current_configuration
from pyracast.plants.preparation import prepare_calibration, request_preparation
from pyracast.regions.models import Region, active_region

MIN_AUTOMATIC_DAYS = 30
MIN_NEW_HOLDOUT_DAYS = 7
MIN_INCUMBENT_IMPROVEMENT = 0.01
MAX_LATEST_HOLDOUT_AGE_DAYS = 7
MAX_EARLIEST_HOLDOUT_AGE_DAYS = 14
MAX_PROMOTION_FAILURES = 3

logger = logging.getLogger(__name__)


def _authorized(plant: Plant, *, lock: bool = False) -> bool:
    actor_id = plant.automatic_calibration_enabled_by_id
    if actor_id is None:
        return False
    members = Membership.objects.filter(
        user_id=actor_id,
        user__is_active=True,
        tenant_id=plant.region.tenant_id,
        role=WorkspaceRole.ADMIN,
    )
    if lock:
        if not User.objects.select_for_update().filter(pk=actor_id, is_active=True).exists():
            return False
        members = members.select_for_update()
    return members.exists()


def _last_attempt(plant: Plant) -> PlantEvent | None:
    return (
        PlantEvent.objects.filter(
            plant=plant,
            details__configuration_id=plant.current_configuration_id,
            action__in=(
                PlantEventAction.CALIBRATION_AUTOMATIC_SKIPPED,
                PlantEventAction.CALIBRATION_AUTOMATICALLY_ACTIVATED,
            ),
        )
        .order_by("-created_at", "-pk")
        .first()
    )


def automation_status(plant: Plant) -> dict[str, Any]:
    last = _last_attempt(plant)
    configuration = plant.current_configuration
    disabled = (
        None
        if plant.automatic_calibration_enabled
        else PlantEvent.objects.filter(
            plant=plant, action=PlantEventAction.CALIBRATION_AUTOMATION_DISABLED
        )
        .order_by("-created_at", "-pk")
        .first()
    )
    return {
        "enabled": plant.automatic_calibration_enabled,
        "authorized": _authorized(plant),
        "configuration_id": plant.current_configuration_id,
        "configuration_current": configuration is not None
        and configuration_is_current(configuration),
        "next_check_at": plant.next_calibration_check_at,
        "disabled_cause": disabled.details.get("cause") if disabled is not None else None,
        "current_calibration": current_selection(plant, timezone.now()),
        "last_attempt": (
            {
                "calibration_id": last.details.get("calibration_id"),
                "status": "activated"
                if last.action == "calibration_automatically_activated"
                else "skipped",
                "reason": last.details.get("reason", "accepted"),
                "created_at": last.created_at,
            }
            if last is not None
            else None
        ),
    }


@transaction.atomic
def automated_plants(tenant_id: int) -> QuerySet[Plant]:
    """The plants an administrator change switches to manual calibration, trashed ones too."""
    return Plant.objects.filter(
        region__tenant_id=tenant_id, automatic_calibration_enabled=True
    ).order_by("pk")


def disable_workspace_automation(actor: User, tenant_id: int) -> None:
    require_platform_admin(actor)
    Tenant.objects.select_for_update().get(pk=tenant_id)
    list(
        Region.objects.select_for_update()
        .filter(tenant_id=tenant_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    plants = automated_plants(tenant_id).select_for_update(of=("self",))
    now = timezone.now()
    for plant in plants:
        set_automation_state(
            plant, actor, enabled=False, cause=AutomationCause.WORKSPACE_DISABLED, now=now
        )


def set_calibration_automation(
    actor: User, tenant_id: int, region_id: int, plant_id: int, *, enabled: bool
) -> Plant:
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        region = Region.objects.active().select_for_update().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.select_for_update().get(pk=plant_id, region=region)
        if enabled:
            current_configuration(plant)
        set_automation_state(
            plant, actor, enabled=enabled, cause=AutomationCause.TOGGLE, now=timezone.now()
        )
        if enabled:
            latest = (
                CalibrationRun.objects.filter(
                    plant=plant, configuration_id=plant.current_configuration_id
                )
                .order_by("-created_at", "-pk")
                .first()
            )
            if (
                latest is not None
                and latest.status == CalibrationStatus.READY
                and selection(plant).active_calibration_id != latest.pk
                and not CalibrationRun.objects.filter(
                    plant=plant, status__in=OPEN_STATUSES
                ).exists()
            ):
                promote_automatic_calibration(plant, latest, timezone.now())
            request_preparation([plant.pk])
        return plant


def record_automatic_skip(plant: Plant, run_id: int | None, reason: str) -> None:
    record_plant_event(
        plant_id=plant.pk,
        actor=None,
        action=PlantEventAction.CALIBRATION_AUTOMATIC_SKIPPED,
        details={
            "calibration_id": run_id,
            "configuration_id": plant.current_configuration_id,
            "reason": reason,
            "origin": "automatic",
            "enabled_by_id": plant.automatic_calibration_enabled_by_id,
        },
    )


def _holdout_dates(run: CalibrationRun) -> list[date]:
    values = run.result.get("holdout_day_dates")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return []
    try:
        return sorted({date.fromisoformat(value) for value in values})
    except ValueError:
        return []


def _promotion_reason(run: CalibrationRun, now: datetime) -> str | None:
    days = run.result.get("valid_days")
    holdout = _holdout_dates(run)
    today = now.astimezone(ZoneInfo(run.binding.timezone)).date()
    if (
        run.assessment != CalibrationAssessment.EVALUATED
        or not isinstance(days, int)
        or isinstance(days, bool)
        or days < MIN_AUTOMATIC_DAYS
        or run.result.get("holdout_days") != MIN_NEW_HOLDOUT_DAYS
        or len(holdout) != MIN_NEW_HOLDOUT_DAYS
        or any(not run.window_start <= day <= run.window_end or day >= today for day in holdout)
    ):
        return "insufficient_automatic_evaluation"
    source_validation = current_source_validation(run.dataset_manifest, run.result, run.binding_id)
    if (
        not source_validation["eligible"]
        or run.result.get("current_source_validation") != source_validation
        or any(
            not run.window_start <= date.fromisoformat(day) <= run.window_end
            for day in cast(list[str], source_validation["valid_day_dates"])
        )
    ):
        return "insufficient_current_source_validation"
    if (today - holdout[-1]).days > MAX_LATEST_HOLDOUT_AGE_DAYS or (
        today - holdout[0]
    ).days > MAX_EARLIEST_HOLDOUT_AGE_DAYS:
        return "stale_automatic_evaluation"
    comparison = run.comparison_activation
    if comparison is not None and comparison.calibration is not None:
        derating = run.result.get("derating")
        if (
            isinstance(derating, dict)
            and derating.get("status") == "suspected"
            and not (run.result.get("candidate_kind") == "identity_recovery" and run.factor == 1.0)
        ):
            return "derating_suspected"
        incumbent = comparison.calibration
        previous = _holdout_dates(incumbent)
        boundary = max(previous) if previous else incumbent.window_end
        if sum(day > boundary for day in holdout) < MIN_NEW_HOLDOUT_DAYS:
            return "awaiting_new_holdout_days"
    candidate, incumbent_metrics = run.result.get("candidate"), run.result.get("incumbent")
    if not isinstance(candidate, dict) or not isinstance(incumbent_metrics, dict):
        return "invalid_incumbent_comparison"
    for metric in ("interval_mae_mw", "daily_mae_mwh"):
        value, reference = _metric(candidate.get(metric)), _metric(incumbent_metrics.get(metric))
        if value is None or reference is None:
            return "invalid_incumbent_comparison"
        tolerance = 1e-12 * max(1.0, abs(reference))
        if value >= reference - tolerance or value > reference * (1 - MIN_INCUMBENT_IMPROVEMENT):
            return "incumbent_improvement_below_margin"
    return None


def _metric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return float(value) if isfinite(value) and value >= 0 else None


def promote_automatic_calibration(plant: Plant, run: CalibrationRun, now: datetime) -> bool:
    if not plant.automatic_calibration_enabled:
        return False
    reason: str | None
    if not _authorized(plant, lock=True):
        reason = "authorization_revoked"
    elif not configuration_is_current(run.configuration):
        reason = "configuration_changed"
    elif run.status != CalibrationStatus.READY:
        reason = f"candidate_{run.status}"
    else:
        reason = activation_blocker(plant, run.configuration, run, now) or _promotion_reason(
            run, now
        )
    if reason is not None:
        record_automatic_skip(plant, run.pk, reason)
        return False
    record_activation(None, plant, run.configuration, run)
    return True


def attempt_promotion(plant: Plant, run: CalibrationRun, now: datetime) -> bool:
    """Promote in a savepoint; a raising promotion is recorded as skip promotion_failed."""
    try:
        with transaction.atomic():
            promote_automatic_calibration(plant, run, now)
    except Exception as error:
        log_failure(logger, "calibration_promotion_failed", error, run=run.pk)
        record_automatic_skip(plant, run.pk, "promotion_failed")
        return False
    return True


def _undecided_candidate(plant: Plant) -> CalibrationRun | None:
    """The latest run of the current configuration while it is ready and its promotion is
    undecided: neither activated nor skipped for a reason. A raising promotion decides nothing
    until it has failed MAX_PROMOTION_FAILURES times."""
    run = (
        CalibrationRun.objects.filter(plant=plant, configuration_id=plant.current_configuration_id)
        .order_by("-created_at", "-pk")
        .first()
    )
    if run is None or run.status != CalibrationStatus.READY:
        return None
    decisions = PlantEvent.objects.filter(
        plant=plant,
        details__calibration_id=run.pk,
        action__in=(
            PlantEventAction.CALIBRATION_AUTOMATIC_SKIPPED,
            PlantEventAction.CALIBRATION_AUTOMATICALLY_ACTIVATED,
        ),
    )
    failed = decisions.filter(
        action=PlantEventAction.CALIBRATION_AUTOMATIC_SKIPPED, details__reason="promotion_failed"
    )
    decided = (
        CalibrationActivation.objects.filter(calibration=run).exists()
        or decisions.exclude(pk__in=failed).exists()
        or failed.count() >= MAX_PROMOTION_FAILURES
    )
    return None if decided else run


def promote_undecided_candidate(plant_id: int, now: datetime) -> bool:
    """Promote a ready candidate whose promotion is undecided; the caller holds the plant lock.

    False while a raising promotion has attempts left: the caller retries later.
    """
    plant = Plant.objects.select_related("region").get(pk=plant_id)
    run = _undecided_candidate(plant) if plant.automatic_calibration_enabled else None
    return run is None or attempt_promotion(plant, run, now) or _undecided_candidate(plant) is None


def prepare_due_calibrations(now: datetime, limit: int = 100) -> list[int]:
    if now.utcoffset() is None or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValidationError("Use an aware timestamp and a limit between 1 and 1000.")
    due = list(
        _due_plants(now)
        .filter(active_region("region__"))
        .order_by("next_calibration_check_at", "pk")
        .values_list("pk", "region_id", "region__tenant_id")[:limit]
    )
    runs = isolate(
        due,
        partial(_prepare_due, now=now),
        logger=logger,
        event="calibration_due_check_failed",
        name=lambda row: f"plant:{row[0]}",
        back_off=partial(_defer_due_check, now=now),
    )
    return [run_id for run_id in runs if run_id is not None]


def _due_plants(now: datetime) -> PlantQuerySet:
    return Plant.objects.filter(
        automatic_calibration_enabled=True, next_calibration_check_at__lte=now
    )


def _prepare_due(row: tuple[int, int, int], now: datetime) -> int | None:
    plant_id, region_id, tenant_id = row
    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=tenant_id)
        region = Region.objects.select_for_update().filter(pk=region_id).first()
        if region is None or region.deleted_at is not None:
            return None
        plant = _due_plants(now).select_for_update().filter(pk=plant_id).first()
        if plant is None:
            return None
        run = None
        if _authorized(plant, lock=True):
            run = prepare_calibration(plant.pk, now)
        elif not _paused(plant):
            record_automatic_skip(plant, None, "authorization_revoked")
        plant.next_calibration_check_at = next_check(plant, now)
        plant.save(update_fields=("next_calibration_check_at",))
        return None if run is None else run.pk


def _paused(plant: Plant) -> bool:
    last = _last_attempt(plant)
    return (
        last is not None
        and last.action == PlantEventAction.CALIBRATION_AUTOMATIC_SKIPPED
        and last.details.get("reason") == "authorization_revoked"
    )


def _defer_due_check(row: tuple[int, int, int], now: datetime) -> None:
    plant = Plant.objects.select_related("region").get(pk=row[0])
    with transaction.atomic():
        if Plant.objects.filter(pk=plant.pk, automatic_calibration_enabled=True).update(
            next_calibration_check_at=next_check(plant, now)
        ):
            record_automatic_skip(plant, None, "preparation_failed")
