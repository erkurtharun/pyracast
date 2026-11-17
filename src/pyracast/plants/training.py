import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.background.failures import log_failure
from pyracast.connections.models import ProviderAccount
from pyracast.plants.calibration_data import build_calibration_dataset, current_source_validation
from pyracast.plants.forecasting import power_limit
from pyracast.plants.incident_policy import incidents_unchanged
from pyracast.plants.measurements import production_identity_current
from pyracast.plants.models import (
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
)
from pyracast.plants.numerics import (
    FitResult,
    fit_calibration,
)
from pyracast.plants.operations import configuration_is_current
from pyracast.plants.preparation import prepare_calibration
from pyracast.plants.transfer import transfer_model
from pyracast.regions.models import Region

LEASE_DURATION = timedelta(minutes=5)
MAX_ATTEMPTS = 3

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Lease:
    run: CalibrationRun
    configuration: PlantConfiguration
    token: uuid.UUID


def _prepare_followup(plant: Plant, run: CalibrationRun, now: datetime) -> None:
    from pyracast.plants.automation import promote_automatic_calibration

    promote_automatic_calibration(plant, run, now)
    prepare_calibration(run.plant_id, now)


def _claim(run_id: int, now: datetime) -> _Lease | None:
    started = time.monotonic()
    identity = CalibrationRun.objects.select_related("plant__region").filter(pk=run_id).first()
    if identity is None:
        return None
    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=identity.plant.region.tenant_id)
        if not Region.objects.select_for_update().filter(pk=identity.plant.region_id).exists():
            return None
        plant = Plant.objects.select_for_update().get(pk=identity.plant_id)
        run = CalibrationRun.objects.select_for_update().get(pk=run_id)
        now += timedelta(seconds=time.monotonic() - started)
        if run.status not in {"pending", "running"}:
            return None
        if (
            run.status == "running"
            and run.lease_expires_at is not None
            and run.lease_expires_at > now
        ):
            return None
        configuration = PlantConfiguration.objects.select_related("binding__provider_account").get(
            pk=run.configuration_id
        )
        if (
            not configuration_is_current(configuration)
            or run.binding_id != configuration.binding_id
            or not production_identity_current(run)
            or not incidents_unchanged(run, now)
        ):
            run.status = "superseded"
        elif run.attempts >= MAX_ATTEMPTS or run.cutoff_at > now:
            run.status = "failed"
            run.last_error = "attempt_limit" if run.attempts >= MAX_ATTEMPTS else "invalid_cutoff"
            logger.warning("calibration_run_failed run=%s reason=%s", run.pk, run.last_error)
        else:
            token = uuid.uuid4()
            run.status = "running"
            run.attempts += 1
            run.lease_token = token
            run.lease_expires_at = now + LEASE_DURATION
            run.save(update_fields=("status", "attempts", "lease_token", "lease_expires_at"))
            return _Lease(run, configuration, token)
        run.completed_at = now
        run.lease_token = None
        run.lease_expires_at = None
        run.save(
            update_fields=(
                "status",
                "last_error",
                "completed_at",
                "lease_token",
                "lease_expires_at",
            )
        )
        _prepare_followup(plant, run, now)
        return None


def _lock_calibration_records(lease: _Lease) -> tuple[Plant, CalibrationRun]:
    Tenant.objects.select_for_update().get(pk=lease.configuration.plant.region.tenant_id)
    Region.objects.select_for_update().get(pk=lease.configuration.binding.region_id)
    plant = Plant.objects.select_for_update().get(pk=lease.run.plant_id)
    run = CalibrationRun.objects.select_for_update().get(pk=lease.run.pk)
    return plant, run


def _publish(lease: _Lease, result: FitResult, manifest: dict[str, object], now: datetime) -> bool:
    started = time.monotonic()
    with transaction.atomic():
        plant, run = _lock_calibration_records(lease)
        ProviderAccount.objects.select_for_update().get(
            pk=lease.configuration.binding.provider_account_id
        )
        now += timedelta(seconds=time.monotonic() - started)
        if (
            run.status != "running"
            or run.lease_token != lease.token
            or run.lease_expires_at is None
            or run.lease_expires_at <= now
        ):
            logger.warning("calibration_lease_lost run=%s", run.pk)
            return False
        configuration = PlantConfiguration.objects.select_related("binding__provider_account").get(
            pk=run.configuration_id
        )
        if (
            not configuration_is_current(configuration)
            or run.binding_id != configuration.binding_id
            or not production_identity_current(run)
            or not incidents_unchanged(run, now)
        ):
            run.status = "superseded"
        else:
            run.status, run.factor, run.assessment = result.status, result.factor, result.assessment
            run.result = result.report
            run.dataset_manifest = manifest
            run.dataset_sha256 = hashlib.sha256(
                json.dumps(
                    manifest, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode()
            ).hexdigest()
        run.completed_at = now
        run.lease_token = None
        run.lease_expires_at = None
        run.last_error = ""
        run.save()
        PlantEvent.objects.create(
            plant_id=run.plant_id,
            actor=None,
            action="calibration_completed",
            details={
                "calibration_id": run.pk,
                "status": run.status,
                "dataset_sha256": run.dataset_sha256,
            },
        )
        _prepare_followup(plant, run, now)
        return run.status != "superseded"


def _fail(lease: _Lease, now: datetime) -> None:
    started = time.monotonic()
    with transaction.atomic():
        plant, run = _lock_calibration_records(lease)
        now += timedelta(seconds=time.monotonic() - started)
        if (
            run.status != "running"
            or run.lease_token != lease.token
            or run.lease_expires_at is None
            or run.lease_expires_at <= now
        ):
            logger.warning("calibration_lease_lost run=%s", run.pk)
            return
        run.status = "failed" if run.attempts >= MAX_ATTEMPTS else "pending"
        run.completed_at = now if run.status == "failed" else None
        run.last_error = "invalid_calibration_data"
        run.lease_token = None
        run.lease_expires_at = None
        run.save(
            update_fields=(
                "status",
                "completed_at",
                "last_error",
                "lease_token",
                "lease_expires_at",
            )
        )
        if run.status == "failed":
            _prepare_followup(plant, run, now)


def run_calibration(run_id: int, now: datetime | None = None) -> bool:
    start = time.monotonic()
    now = timezone.now() if now is None else now
    if now.utcoffset() is None:
        raise ValidationError("An aware timestamp is required.")
    lease = _claim(run_id, now)
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
        result.report["transfer_model"] = transfer_model(lease.configuration)
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
        return _publish(
            lease, result, dataset.manifest, now + timedelta(seconds=time.monotonic() - start)
        )
    except (ValueError, ArithmeticError, TypeError, KeyError) as error:
        log_failure(
            logger,
            "calibration_attempt_failed",
            error,
            run=run_id,
            attempt=lease.run.attempts,
        )
        _fail(lease, now + timedelta(seconds=time.monotonic() - start))
        return False


def pending_calibration_ids(now: datetime, limit: int = 100) -> list[int]:
    if now.utcoffset() is None or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValidationError("Use an aware timestamp and a limit between 1 and 1000.")
    return list(
        CalibrationRun.objects.filter(
            Q(status="pending")
            | Q(status="running", lease_expires_at__lte=now)
            | Q(status="running", lease_expires_at__isnull=True),
            plant__region__deleted_at__isnull=True,
        )
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
