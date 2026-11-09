import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pyracast.connections.models import ProviderAccount
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecasting import (
    SELECTION_POLICY,
    power_intervals,
    power_limit,
    select_snapshot,
)
from pyracast.plants.models import (
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
    ProductionObservation,
)
from pyracast.plants.numerics import CalibrationSample, FitResult, PowerInterval, fit_calibration
from pyracast.plants.operations import configuration_is_current
from pyracast.regions.models import Region

LEASE_DURATION = timedelta(minutes=5)
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class _Lease:
    run: CalibrationRun
    configuration: PlantConfiguration
    token: uuid.UUID


def _claim(run_id: int, now: datetime) -> _Lease | None:
    started = time.monotonic()
    identity = CalibrationRun.objects.select_related("plant").get(pk=run_id)
    with transaction.atomic():
        Region.objects.select_for_update().get(pk=identity.plant.region_id)
        Plant.objects.select_for_update().get(pk=identity.plant_id)
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
        ):
            run.status = "superseded"
        elif run.attempts >= MAX_ATTEMPTS or run.cutoff_at > now:
            run.status = "failed"
            run.last_error = "attempt_limit" if run.attempts >= MAX_ATTEMPTS else "invalid_cutoff"
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
        return None


def _dataset(
    lease: _Lease,
) -> tuple[tuple[CalibrationSample, ...], dict[str, object], dict[str, object]]:
    run, configuration = lease.run, lease.configuration
    zone = ZoneInfo(configuration.binding.timezone)
    start, _ = local_day_bounds(run.window_start, str(zone))
    _, end = local_day_bounds(run.window_end, str(zone))
    candidates = ProductionObservation.objects.filter(
        plant_id=run.plant_id,
        configuration=configuration,
        starts_at__gte=start,
        ends_at__lte=end,
    )
    late = candidates.filter(
        Q(recorded_at__gt=run.cutoff_at) | Q(ends_at__gt=run.cutoff_at)
    ).count()
    observations = candidates.filter(
        recorded_at__lte=run.cutoff_at, ends_at__lte=run.cutoff_at
    ).order_by("starts_at", "pk")
    samples = []
    entries: list[dict[str, object]] = []
    sources: dict[str, dict[str, object] | None] = {}
    native_by_day: dict[str, tuple[PowerInterval, ...]] = {}
    unmatched = 0
    for observation in observations:
        day = observation.starts_at.astimezone(zone).date()
        key = day.isoformat()
        if key not in sources:
            day_start, _ = local_day_bounds(day, str(zone))
            snapshot = select_snapshot(configuration, day, min(day_start, run.cutoff_at))
            sources[key] = (
                None
                if snapshot is None
                else {
                    "snapshot_id": snapshot.pk,
                    "collected_at": snapshot.collected_at.isoformat(),
                    "scheduled_slot": snapshot.job.scheduled_slot.isoformat(),
                    "late": snapshot.collected_at > snapshot.job.scheduled_slot,
                    "lead_hours": (day_start - snapshot.collected_at).total_seconds() / 3600,
                    "issued_at": snapshot.issued_at.isoformat() if snapshot.issued_at else None,
                }
            )
            native_by_day[key] = (
                () if snapshot is None else power_intervals(snapshot, configuration)
            )
        pieces = tuple(
            PowerInterval(
                max(piece.start, observation.starts_at),
                min(piece.end, observation.ends_at),
                piece.power_mw,
            )
            for piece in native_by_day[key]
            if piece.start < observation.ends_at and piece.end > observation.starts_at
        )
        entries.append(
            {
                "observation_id": observation.pk,
                "start": observation.starts_at.isoformat(),
                "end": observation.ends_at.isoformat(),
                "energy_mwh": observation.energy_mwh,
                "quality": observation.quality,
                "source": observation.source,
                "recorded_at": observation.recorded_at.isoformat(),
                "day": key,
                "intervals": [
                    {
                        "start": piece.start.isoformat(),
                        "end": piece.end.isoformat(),
                        "power_mw": piece.power_mw,
                    }
                    for piece in pieces
                ],
            }
        )
        if sources[key] is None:
            unmatched += 1
        samples.append(
            CalibrationSample(
                observation.starts_at,
                observation.ends_at,
                observation.energy_mwh,
                pieces,
                observation.quality,
            )
        )
    manifest: dict[str, object] = {
        "configuration_id": configuration.pk,
        "binding_id": configuration.binding_id,
        "dc_capacity_mw": configuration.dc_capacity_mw,
        "limit_mw": power_limit(configuration),
        "measurement_basis": configuration.measurement_basis,
        "cutoff_at": run.cutoff_at.isoformat(),
        "window_start": run.window_start.isoformat(),
        "window_end": run.window_end.isoformat(),
        "selection_policy": SELECTION_POLICY,
        "sources": sources,
        "observations": entries,
    }
    report: dict[str, object] = {
        "recorded_after_cutoff": late,
        "unmatched_observations": unmatched,
        "unmatched_days": [day for day, source in sources.items() if source is None],
        "selected_observations": len(entries),
    }
    return tuple(samples), manifest, report


def _publish(lease: _Lease, result: FitResult, manifest: dict[str, object], now: datetime) -> bool:
    started = time.monotonic()
    with transaction.atomic():
        Region.objects.select_for_update().get(pk=lease.configuration.binding.region_id)
        Plant.objects.select_for_update().get(pk=lease.run.plant_id)
        run = CalibrationRun.objects.select_for_update().get(pk=lease.run.pk)
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
            return False
        configuration = PlantConfiguration.objects.select_related("binding__provider_account").get(
            pk=run.configuration_id
        )
        if (
            not configuration_is_current(configuration)
            or run.binding_id != configuration.binding_id
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
        return run.status != "superseded"


def _fail(lease: _Lease, now: datetime) -> None:
    started = time.monotonic()
    with transaction.atomic():
        run = CalibrationRun.objects.select_for_update().get(pk=lease.run.pk)
        now += timedelta(seconds=time.monotonic() - started)
        if (
            run.status != "running"
            or run.lease_token != lease.token
            or run.lease_expires_at is None
            or run.lease_expires_at <= now
        ):
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


def run_calibration(run_id: int, now: datetime | None = None) -> bool:
    start = time.monotonic()
    now = timezone.now() if now is None else now
    if now.utcoffset() is None:
        raise ValidationError("An aware timestamp is required.")
    lease = _claim(run_id, now)
    if lease is None:
        return False
    try:
        samples, manifest, selection = _dataset(lease)
        result = fit_calibration(
            samples,
            timezone=lease.configuration.binding.timezone,
            normalization_mw=lease.configuration.dc_capacity_mw,
            limit_mw=power_limit(lease.configuration),
        )
        result.report["selection"] = selection
        return _publish(lease, result, manifest, now + timedelta(seconds=time.monotonic() - start))
    except ValueError, ArithmeticError, TypeError, KeyError:
        _fail(lease, now + timedelta(seconds=time.monotonic() - start))
        return False


def pending_calibration_ids(now: datetime, limit: int = 100) -> list[int]:
    if now.utcoffset() is None or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValidationError("Use an aware timestamp and a limit between 1 and 1000.")
    return list(
        CalibrationRun.objects.filter(
            Q(status="pending")
            | Q(status="running", lease_expires_at__lte=now)
            | Q(status="running", lease_expires_at__isnull=True)
        )
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
