from datetime import datetime
from math import isclose, isfinite
from typing import Any, cast

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pyracast.accounts.authorization import require_platform_admin
from pyracast.calculation import calculate
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.jobs import binding_is_current
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
    ReferenceCapacityNormalization,
)
from pyracast.forecasts.sources import snapshot_data
from pyracast.regions.models import AuditEvent, Region

NORMALIZATION_POLICY = "authorized-reference-capacity-scale-v1"


def _validate_bindings(source: SiteBinding, target: SiteBinding) -> float:
    account = target.provider_account
    if (
        source.pk == target.pk
        or not binding_is_current(target)
        or account.provider != "solcast"
        or not account.authorization_reference
        or account.authorized_by_id is None
        or target.validated_at is None
        or source.validated_at is None
        or source.health != "healthy"
        or not source.capacity_semantics_verified
        or not target.capacity_semantics_verified
        or not source.settings_verification_reference
        or not target.settings_verification_reference
    ):
        raise ValidationError(
            "Authorized and verified source and current target bindings are required."
        )
    fields = (
        "region_id",
        "provider_account_id",
        "site_id",
        "latitude",
        "longitude",
        "timezone",
        "azimuth_degrees",
        "losses_percent",
        "installation_date",
        "power_basis",
    )
    if (
        any(getattr(source, field) != getattr(target, field) for field in fields)
        or target.power_basis != "ac"
        or not all(
            isfinite(value) and value > 0 for value in (source.capacity_kw, target.capacity_kw)
        )
        or source.capacity_kw == target.capacity_kw
        # An undeclared DC capacity leaves the provider's implicit DC/AC ratio in effect.
        or source.dc_capacity_kw is None
        or target.dc_capacity_kw is None
        or not isclose(
            source.dc_capacity_kw / source.capacity_kw,
            target.dc_capacity_kw / target.capacity_kw,
            rel_tol=1e-12,
        )
    ):
        raise ValidationError(
            "The bindings must differ only in their declared reference capacity scale."
        )
    factor = target.capacity_kw / source.capacity_kw
    if not isfinite(factor) or factor <= 0:
        raise ValidationError("The capacity scale must be finite and positive.")
    return factor


def _scaled_intervals(rows: list[dict[str, Any]], factor: float) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        scaled = row.copy()
        power = row.get("power_kw")
        if power is not None:
            if isinstance(power, bool) or not isinstance(power, int | float) or power < 0:
                raise ValidationError("Reference power must be nonnegative numeric kW or missing.")
            value = power * factor
            if not isfinite(value):
                raise ValidationError("Scaled reference power must be finite.")
            scaled["power_kw"] = value
        result.append(scaled)
    return result


def normalization_provenance(snapshot: ForecastSnapshot) -> dict[str, object] | None:
    record = getattr(snapshot, "capacity_normalization", None)
    if record is None:
        return None
    return {
        "id": record.pk,
        "policy": NORMALIZATION_POLICY,
        "source_snapshot_id": record.source_snapshot_id,
        "source_binding_id": record.source_snapshot.job.binding_id,
        "source_capacity_kw": record.source_capacity_kw,
        "source_available_at": cast(datetime, record.source_snapshot.available_at).isoformat(),
        "target_capacity_kw": record.target_capacity_kw,
        "factor": record.factor,
        "authorized_by_id": record.authorized_by_id,
        "authorization_reference": record.authorization_reference,
        "normalized_at": record.created_at.isoformat(),
        "raw_payload_capacity_kw": record.source_capacity_kw,
    }


def _copy_snapshot(
    source: ForecastSnapshot,
    target: SiteBinding,
    actor: User,
    reference: str,
    factor: float,
    moment: datetime,
) -> ForecastSnapshot:
    job = CollectionJob.objects.create(
        binding=target,
        endpoint="scaled_" + source.job.endpoint,
        scheduled_slot=source.job.scheduled_slot,
        target_date=source.job.target_date,
        status="succeeded",
        available_at=moment,
    )
    snapshot = ForecastSnapshot.objects.create(
        job=job,
        binding=target,
        raw_payload=source.raw_payload,
        normalized_intervals=_scaled_intervals(source.normalized_intervals, factor),
        collected_at=source.collected_at,
        issued_at=source.issued_at,
        source_kind=source.source_kind,
        coverage_start=source.coverage_start,
        coverage_end=source.coverage_end,
        available_at=moment,
        weather_payload=source.weather_payload,
        weather_error=source.weather_error,
    )
    ReferenceCapacityNormalization.objects.create(
        source_snapshot=source,
        snapshot=snapshot,
        target_binding=target,
        source_capacity_kw=source.job.binding.capacity_kw,
        target_capacity_kw=target.capacity_kw,
        factor=factor,
        authorized_by=actor,
        authorization_reference=reference,
        created_at=moment,
    )
    payload = calculate(target, job.target_date, snapshot_data(snapshot))
    payload["reference_capacity_normalization"] = normalization_provenance(snapshot)
    ForecastResult.objects.create(snapshot=snapshot, payload=payload, published_at=moment)
    return snapshot


def normalize_reference_capacity(
    actor: User,
    tenant_id: int,
    region_id: int,
    source_binding_id: int,
    target_binding_id: int,
    authorization_reference: str,
) -> tuple[int, ...]:
    require_platform_admin(actor)
    reference = authorization_reference.strip()
    if not reference or len(reference) > 1000:
        raise ValidationError("A bounded capacity-only equivalence authorization is required.")
    with transaction.atomic():
        region = Region.objects.select_for_update().get(
            pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True
        )
        target = SiteBinding.objects.select_related("provider_account").get(
            pk=target_binding_id, region=region
        )
        account = ProviderAccount.objects.select_for_update().get(pk=target.provider_account_id)
        SiteBinding.objects.get(pk=source_binding_id, region=region)
        bindings = {
            row.pk: row
            for row in SiteBinding.objects.select_for_update()
            .filter(pk__in=(source_binding_id, target_binding_id), region=region)
            .order_by("pk")
        }
        source = bindings[source_binding_id]
        target = bindings[target_binding_id]
        target.provider_account = account
        factor = _validate_bindings(source, target)
        moment = timezone.now()
        snapshots = (
            ForecastSnapshot.objects.select_related("job__binding")
            .filter(
                Q(job__endpoint="estimated_actuals", source_kind="estimated_actuals")
                | Q(job__endpoint="forecasts", source_kind="forecast"),
                job__binding=source,
                job__status="succeeded",
                job__reference_import__isnull=True,
                capacity_normalization__isnull=True,
                collected_at__lte=moment,
                available_at__lte=moment,
            )
            .filter(Q(issued_at__isnull=True) | Q(issued_at__lte=moment))
            .order_by("pk")
        )
        ids = []
        changed = False
        for snapshot in snapshots:
            previous = ReferenceCapacityNormalization.objects.filter(
                source_snapshot=snapshot, target_binding=target
            ).first()
            if previous is not None:
                ids.append(previous.snapshot_id)
                continue
            copied = _copy_snapshot(snapshot, target, actor, reference, factor, moment)
            ids.append(copied.pk)
            changed = True
        if changed:
            AuditEvent.objects.create(
                actor=actor,
                region=region,
                action="reference_capacity_normalized",
                revision=region.revision,
            )
    return tuple(ids)
