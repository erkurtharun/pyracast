import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.background.claims import claim_unless_contended
from pyracast.background.clock import Clock
from pyracast.background.deadlines import ACTORS
from pyracast.background.failures import log_failure
from pyracast.background.lease import claimable, held, owns
from pyracast.background.retry_policy import CALIBRATION
from pyracast.connections.models import ProviderAccount
from pyracast.plants.automation import Enabler, attempt_promotion, lock_enabler
from pyracast.plants.calibration_data import build_calibration_dataset, current_source_validation
from pyracast.plants.calibration_runs import (
    OPEN_STATUSES,
    SupersedeReason,
    finish_run,
    requeue_run,
    start_run,
)
from pyracast.plants.forecasting import power_limit
from pyracast.plants.incident_policy import incidents_unchanged
from pyracast.plants.measurements import production_identity_current
from pyracast.plants.models import (
    CalibrationError,
    CalibrationRun,
    CalibrationStatus,
    Plant,
    PlantConfiguration,
    active_plant,
)
from pyracast.plants.numerics import (
    FitResult,
    fit_calibration,
)
from pyracast.plants.operations import configuration_is_current
from pyracast.plants.preparation import request_preparation
from pyracast.regions.models import Region

LEASE_DURATION = ACTORS["calibrate_plant"].lease
WAITING = (CalibrationStatus.PENDING,)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Lease:
    run: CalibrationRun
    configuration: PlantConfiguration
    token: uuid.UUID
    tenant_id: int


def _follow_up(plant: Plant, run: CalibrationRun, now: datetime, enabler: Enabler) -> None:
    attempt_promotion(plant, run, now, enabler)
    request_preparation([run.plant_id], now)


def _supersede_reason(
    run: CalibrationRun, configuration: PlantConfiguration, now: datetime
) -> SupersedeReason | None:
    if not configuration_is_current(configuration):
        return SupersedeReason.CONFIGURATION_CHANGED
    if run.binding_id != configuration.binding_id:
        return SupersedeReason.BINDING_CHANGED
    if not production_identity_current(run):
        return SupersedeReason.PRODUCTION_SOURCE_CHANGED
    if not incidents_unchanged(run, now):
        return SupersedeReason.OUTAGE_HISTORY_CHANGED
    return None


def _claim(run_id: int, now: datetime) -> _Lease | None:
    clock = Clock(now)
    identity = CalibrationRun.objects.select_related("plant__region").filter(pk=run_id).first()
    if identity is None:
        return None
    tenant_id = identity.plant.region.tenant_id
    with transaction.atomic():
        enabler = lock_enabler(identity.plant_id)
        Tenant.objects.select_for_update().get(pk=tenant_id)
        if not Region.objects.select_for_update().filter(pk=identity.plant.region_id).exists():
            return None
        plant = Plant.objects.select_for_update().get(pk=identity.plant_id)
        run = CalibrationRun.objects.select_for_update().get(pk=run_id)
        now = clock.now()
        if run.status not in OPEN_STATUSES or held(run, CalibrationStatus.RUNNING, now):
            return None
        configuration = PlantConfiguration.objects.select_related("binding__provider_account").get(
            pk=run.configuration_id
        )
        reason = _supersede_reason(run, configuration, now)
        if reason is not None:
            finish_run(run, CalibrationStatus.SUPERSEDED, now, reason)
        elif CALIBRATION.exhausted(run.attempts) or run.cutoff_at > now + LEASE_DURATION:
            error = (
                CalibrationError.ATTEMPT_LIMIT
                if CALIBRATION.exhausted(run.attempts)
                else CalibrationError.INVALID_CUTOFF
            )
            logger.warning("calibration_run_failed run=%s reason=%s", run.pk, error)
            finish_run(run, CalibrationStatus.FAILED, now, error)
        elif run.cutoff_at > now:
            return None
        else:
            return _Lease(run, configuration, start_run(run, now, LEASE_DURATION), tenant_id)
        _follow_up(plant, run, now, enabler)
        return None


def _lock_calibration_records(lease: _Lease) -> tuple[Enabler, Plant, CalibrationRun]:
    enabler = lock_enabler(lease.run.plant_id)
    Tenant.objects.select_for_update().get(pk=lease.tenant_id)
    Region.objects.select_for_update().get(pk=lease.configuration.binding.region_id)
    plant = Plant.objects.select_for_update().get(pk=lease.run.plant_id)
    run = CalibrationRun.objects.select_for_update().get(pk=lease.run.pk)
    return enabler, plant, run


def _holds_lease(run: CalibrationRun, lease: _Lease, now: datetime) -> bool:
    if owns(run, lease.token, CalibrationStatus.RUNNING, now):
        return True
    logger.warning("calibration_lease_lost run=%s", run.pk)
    return False


def _publish(lease: _Lease, result: FitResult, manifest: dict[str, object], now: datetime) -> bool:
    clock = Clock(now)
    with transaction.atomic():
        enabler, plant, run = _lock_calibration_records(lease)
        ProviderAccount.objects.select_for_update().get(
            pk=lease.configuration.binding.provider_account_id
        )
        now = clock.now()
        if not _holds_lease(run, lease, now):
            return False
        configuration = PlantConfiguration.objects.select_related("binding__provider_account").get(
            pk=run.configuration_id
        )
        reason = _supersede_reason(run, configuration, now)
        if reason is None:
            run.factor, run.assessment = result.factor, result.assessment
            run.result = result.report
            run.dataset_manifest = manifest
            run.dataset_sha256 = hashlib.sha256(
                json.dumps(
                    manifest, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode()
            ).hexdigest()
        finish_run(
            run, CalibrationStatus.SUPERSEDED if reason else result.status, now, reason or ""
        )
        _follow_up(plant, run, now, enabler)
        return reason is None


def _fail(lease: _Lease, now: datetime) -> None:
    clock = Clock(now)
    with transaction.atomic():
        enabler, plant, run = _lock_calibration_records(lease)
        now = clock.now()
        if not _holds_lease(run, lease, now):
            return
        if not CALIBRATION.exhausted(run.attempts):
            requeue_run(run, CalibrationError.INVALID_CALIBRATION_DATA, now)
            return
        finish_run(run, CalibrationStatus.FAILED, now, CalibrationError.INVALID_CALIBRATION_DATA)
        _follow_up(plant, run, now, enabler)


def run_calibration(run_id: int, now: datetime | None = None) -> bool:
    clock = Clock(timezone.now() if now is None else now)
    lease = claim_unless_contended("calibrate_plant", run_id, lambda: _claim(run_id, clock.entered))
    if lease is None:
        return False
    try:
        dataset = build_calibration_dataset(lease.run, lease.configuration)
        fit_started = time.monotonic()
        result = fit_calibration(
            dataset.samples,
            timezone=lease.configuration.binding.timezone,
            normalization_mw=lease.configuration.dc_capacity_mw,
            limit_mw=power_limit(lease.configuration),
            incumbent_factor=dataset.incumbent_factor,
            incumbent_is_active=dataset.incumbent_is_active,
            required_windows=dataset.required_windows,
        )
        logger.info(
            "calibration_fit_completed run=%s seconds=%.3f",
            run_id,
            time.monotonic() - fit_started,
        )
        result.report["comparison_activation_id"] = lease.run.comparison_activation_id
        result.report["transfer_model"] = dataset.transfer_model
        result.report["selection"] = dataset.selection
        result.report["provisional_production"] = dataset.selection["provisional_production"]
        result.report["production_sources"] = dataset.selection["production_sources"]
        result.report["current_source_validation"] = current_source_validation(
            dataset.manifest, result.report, lease.run.binding_id
        )
        declared_day = (
            lease.configuration.created_at.astimezone(
                ZoneInfo(lease.configuration.binding.timezone)
            )
            .date()
            .isoformat()
        )
        dataset.manifest["retrospective_configuration_days"] = sum(
            day < declared_day
            for day in cast(list[str], result.report.get("included_day_dates", []))
        )
        return _publish(lease, result, dataset.manifest, clock.now())
    except (ValueError, ArithmeticError, TypeError, KeyError) as error:
        log_failure(
            logger,
            "calibration_attempt_failed",
            error,
            run=run_id,
            attempt=lease.run.attempts,
        )
        _fail(lease, clock.now())
        return False


def pending_calibration_ids(now: datetime, limit: int = 100) -> list[int]:
    if now.utcoffset() is None or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValidationError("Use an aware timestamp and a limit between 1 and 1000.")
    return list(
        CalibrationRun.objects.filter(
            active_plant("plant__"),
            Q(status__in=WAITING) | claimable(CalibrationStatus.RUNNING, now),
        )
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
