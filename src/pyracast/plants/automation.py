from datetime import date, datetime, timedelta
from math import isfinite
from typing import Any, cast
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pyracast.accounts.authorization import require_platform_admin, tenant_membership
from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.irradiance import local_day_bounds
from pyracast.plants.calibration_data import current_source_validation
from pyracast.plants.models import CalibrationRun, Plant, PlantEvent
from pyracast.plants.operations import _activate, _validate_activation, configuration_is_current
from pyracast.plants.preparation import prepare_calibration
from pyracast.regions.models import Region

MIN_AUTOMATIC_DAYS = 30
MIN_NEW_HOLDOUT_DAYS = 7
MIN_INCUMBENT_IMPROVEMENT = 0.01
MAX_LATEST_HOLDOUT_AGE_DAYS = 7
MAX_EARLIEST_HOLDOUT_AGE_DAYS = 14


def _next_check(plant: Plant, now: datetime) -> datetime:
    tomorrow = now.astimezone(ZoneInfo(plant.region.timezone)).date() + timedelta(days=1)
    return local_day_bounds(tomorrow, plant.region.timezone)[0]


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


def automation_status(plant: Plant) -> dict[str, Any]:
    last = (
        PlantEvent.objects.filter(
            plant=plant,
            details__configuration_id=plant.current_configuration_id,
            action__in=("calibration_automatic_skipped", "calibration_automatically_activated"),
        )
        .order_by("-created_at", "-pk")
        .first()
    )
    return {
        "enabled": plant.automatic_calibration_enabled,
        "authorized": _authorized(plant),
        "configuration_id": plant.current_configuration_id,
        "next_check_at": plant.next_calibration_check_at,
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


def update_automation(plant: Plant, actor: User, *, enabled: bool, now: datetime) -> None:
    if plant.automatic_calibration_enabled == enabled and (
        not enabled or plant.automatic_calibration_enabled_by_id == actor.pk
    ):
        return
    plant.automatic_calibration_enabled = enabled
    plant.automatic_calibration_enabled_by = actor if enabled else None
    plant.next_calibration_check_at = _next_check(plant, now) if enabled else None
    plant.save(
        update_fields=(
            "automatic_calibration_enabled",
            "automatic_calibration_enabled_by",
            "next_calibration_check_at",
        )
    )
    PlantEvent.objects.create(
        plant=plant,
        actor=actor,
        action="calibration_automation_enabled" if enabled else "calibration_automation_disabled",
        details={"configuration_id": plant.current_configuration_id},
    )


@transaction.atomic
def disable_workspace_automation(actor: User, tenant_id: int) -> None:
    require_platform_admin(actor)
    Tenant.objects.select_for_update().get(pk=tenant_id)
    region_ids = list(
        Region.objects.select_for_update()
        .filter(tenant_id=tenant_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    plants = (
        Plant.objects.select_for_update()
        .filter(region_id__in=region_ids, automatic_calibration_enabled=True)
        .order_by("pk")
    )
    now = timezone.now()
    for plant in plants:
        update_automation(plant, actor, enabled=False, now=now)


def set_calibration_automation(
    actor: User, tenant_id: int, region_id: int, plant_id: int, *, enabled: bool
) -> Plant:
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        region = Region.objects.select_for_update().get(
            pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True
        )
        plant = Plant.objects.select_for_update().get(pk=plant_id, region=region)
        if enabled and (
            plant.current_configuration is None
            or not configuration_is_current(plant.current_configuration)
        ):
            raise ValidationError(
                "Automatic calibration requires the current approved configuration."
            )
        update_automation(plant, actor, enabled=enabled, now=timezone.now())
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
                and latest.status == "ready"
                and not CalibrationRun.objects.filter(
                    plant=plant, status__in=("pending", "running")
                ).exists()
            ):
                promote_automatic_calibration(plant, latest, timezone.now())
            prepare_calibration(plant.pk)
        return plant


def _skip(plant: Plant, run_id: int | None, reason: str) -> None:
    PlantEvent.objects.create(
        plant=plant,
        actor=None,
        action="calibration_automatic_skipped",
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
        run.assessment != "evaluated"
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
    elif run.status != "ready":
        reason = f"candidate_{run.status}"
    else:
        try:
            _validate_activation(plant, run.configuration, run, now)
        except ValidationError as exc:
            reason = exc.messages[0]
        else:
            reason = _promotion_reason(run, now)
    if reason is not None:
        _skip(plant, run.pk, reason)
        return False
    _activate(None, plant, run.configuration, run)
    return True


def prepare_due_calibrations(now: datetime, limit: int = 100) -> list[int]:
    if now.utcoffset() is None or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValidationError("Use an aware timestamp and a limit between 1 and 1000.")
    due = list(
        Plant.objects.filter(
            Q(next_calibration_check_at__isnull=True) | Q(next_calibration_check_at__lte=now),
            automatic_calibration_enabled=True,
            region__deleted_at__isnull=True,
        )
        .order_by("next_calibration_check_at", "pk")
        .values_list("pk", "region_id", "region__tenant_id")[:limit]
    )
    identifiers = []
    for plant_id, region_id, tenant_id in due:
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=tenant_id)
            region = Region.objects.select_for_update().filter(pk=region_id).first()
            if region is None or region.deleted_at is not None:
                continue
            plant = Plant.objects.select_for_update().filter(pk=plant_id).first()
            if (
                plant is None
                or not plant.automatic_calibration_enabled
                or (
                    plant.next_calibration_check_at is not None
                    and plant.next_calibration_check_at > now
                )
            ):
                continue
            if _authorized(plant, lock=True):
                run = prepare_calibration(plant.pk, now)
                if run is not None:
                    identifiers.append(run.pk)
            else:
                _skip(plant, None, "authorization_revoked")
            plant.next_calibration_check_at = _next_check(plant, now)
            plant.save(update_fields=("next_calibration_check_at",))
    return identifiers
