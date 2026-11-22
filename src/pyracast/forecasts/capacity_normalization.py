from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from math import isfinite
from typing import Any, cast

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext as _

from pyracast.accounts.authorization import require_platform_admin
from pyracast.calculation import calculate
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobStatus,
    ForecastResult,
    ForecastSnapshot,
    ReferenceCapacityNormalization,
    SnapshotSourceKind,
)
from pyracast.forecasts.projections import prepare_daily_payloads
from pyracast.forecasts.sources import snapshot_data
from pyracast.forecasts.trust import normalization_problems, problem_messages
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction, Region

NORMALIZATION_POLICY = "authorized-reference-capacity-scale-v1"
_CHUNK = 50


@dataclass(frozen=True)
class NormalizationOutcome:
    source_binding_id: int
    target_binding_id: int
    source_capacity_kw: float
    target_capacity_kw: float
    factor: float
    created: tuple[int, ...]
    reused: tuple[int, ...]
    by_endpoint: dict[str, int]
    first_target_date: date | None
    last_target_date: date | None
    predecessors: tuple[int, ...]
    dry_run: bool = False

    @property
    def snapshot_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self.created + self.reused))


@dataclass
class _Copy:
    source: ForecastSnapshot
    job: CollectionJob
    snapshot: ForecastSnapshot
    payload: dict[str, Any] | None = None
    daily_payloads: dict[str, dict[str, object]] = field(default_factory=dict)


def _scaled_intervals(rows: list[dict[str, Any]], factor: float) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        scaled = row.copy()
        power = row.get("power_kw")
        if power is not None:
            if isinstance(power, bool) or not isinstance(power, int | float) or power < 0:
                raise ValidationError(
                    _("Reference power must be nonnegative numeric kW or missing.")
                )
            value = power * factor
            if not isfinite(value):
                raise ValidationError(_("Scaled reference power must be finite."))
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


def _checked_factor(source: SiteBinding, target: SiteBinding) -> float:
    problems = normalization_problems(source, target)
    if problems:
        raise ValidationError(problem_messages(problems))
    return target.capacity_kw / source.capacity_kw


def _bindings(
    region: Region, source_id: int, target_id: int, *, lock: bool = False
) -> tuple[SiteBinding, SiteBinding]:
    rows = (
        SiteBinding.objects.select_related("provider_account")
        .filter(pk__in=(source_id, target_id), region=region)
        .order_by("pk")
    )
    found = {row.pk: row for row in (rows.select_for_update(of=("self",)) if lock else rows)}
    if source_id not in found or target_id not in found:
        raise SiteBinding.DoesNotExist
    for binding in found.values():
        binding.region = region
    return found[source_id], found[target_id]


def _eligible(source: SiteBinding, moment: datetime) -> QuerySet[ForecastSnapshot]:
    return ForecastSnapshot.objects.filter(
        Q(
            job__endpoint=CollectionEndpoint.ESTIMATED_ACTUALS,
            source_kind=SnapshotSourceKind.ESTIMATED_ACTUALS,
        )
        | Q(job__endpoint=CollectionEndpoint.FORECASTS, source_kind=SnapshotSourceKind.FORECAST),
        Q(issued_at__isnull=True) | Q(issued_at__lte=moment),
        job__binding=source,
        job__status=CollectionJobStatus.SUCCEEDED,
        job__reference_import__isnull=True,
        capacity_normalization__isnull=True,
        collected_at__lte=moment,
        available_at__lte=moment,
    )


def _prepared(
    source: ForecastSnapshot, target: SiteBinding, factor: float, moment: datetime
) -> _Copy:
    job = CollectionJob(
        binding=target,
        endpoint="scaled_" + source.job.endpoint,
        scheduled_for=source.job.scheduled_for,
        target_date=source.job.target_date,
        status=CollectionJobStatus.SUCCEEDED,
        available_at=moment,
    )
    snapshot = ForecastSnapshot(
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
    )
    copy = _Copy(source, job, snapshot)
    if source.job.endpoint == CollectionEndpoint.FORECASTS:
        data = snapshot_data(snapshot)
        copy.payload = calculate(target, job.target_date, data)
        copy.daily_payloads = prepare_daily_payloads(
            target, job.target_date, data, calculate, copy.payload
        )
    return copy


def _write(
    copies: list[_Copy],
    target: SiteBinding,
    actor: User,
    reference: str,
    factor: float,
    moment: datetime,
) -> tuple[int, ...]:
    CollectionJob.objects.bulk_create([copy.job for copy in copies])
    ForecastSnapshot.objects.bulk_create([copy.snapshot for copy in copies])
    records = ReferenceCapacityNormalization.objects.bulk_create(
        [
            ReferenceCapacityNormalization(
                source_snapshot=copy.source,
                snapshot=copy.snapshot,
                target_binding=target,
                source_capacity_kw=copy.source.job.binding.capacity_kw,
                target_capacity_kw=target.capacity_kw,
                factor=factor,
                authorized_by=actor,
                authorization_reference=reference,
                created_at=moment,
            )
            for copy in copies
        ]
    )
    results = []
    for copy, record in zip(copies, records, strict=True):
        if copy.payload is None:
            continue
        copy.snapshot.capacity_normalization = record
        provenance = {"reference_capacity_normalization": normalization_provenance(copy.snapshot)}
        results.append(
            ForecastResult(
                snapshot=copy.snapshot,
                payload=copy.payload | provenance,
                daily_payloads={
                    day: payload | provenance for day, payload in copy.daily_payloads.items()
                },
                published_at=moment,
            )
        )
    ForecastResult.objects.bulk_create(results)
    return tuple(copy.snapshot.pk for copy in copies)


def normalize_reference_capacity(
    actor: User,
    tenant_id: int,
    region_id: int,
    source_binding_id: int,
    target_binding_id: int | None,
    authorization_reference: str,
    *,
    dry_run: bool = False,
) -> NormalizationOutcome:
    """Copies the source binding's archive onto the target at the target's capacity scale.
    Preparation runs without locks; one short transaction re-checks every trust gate under the
    region, account and binding locks and writes only sources not yet copied, so a replay
    writes nothing."""
    require_platform_admin(actor)
    reference = authorization_reference.strip()
    if not reference or len(reference) > 1000:
        raise ValidationError(_("A bounded capacity-only equivalence authorization is required."))
    region = Region.objects.active().get(pk=region_id, tenant_id=tenant_id)
    target_id = target_binding_id or region.approved_binding_id
    if target_id is None:
        raise ValidationError(_("The plant has no approved connection to normalize onto."))
    source, target = _bindings(region, source_binding_id, target_id)
    factor = _checked_factor(source, target)
    moment = timezone.now()
    eligible = _eligible(source, moment)
    copies = ReferenceCapacityNormalization.objects.filter(target_binding=target)
    reused = tuple(
        copies.filter(source_snapshot__in=eligible)
        .order_by("snapshot_id")
        .values_list("snapshot_id", flat=True)
    )
    planned = [
        _prepared(snapshot, target, factor, moment)
        for snapshot in eligible.exclude(Exists(copies.filter(source_snapshot=OuterRef("pk"))))
        .select_related("job__binding")
        .order_by("pk")
        .iterator(chunk_size=_CHUNK)
    ]
    created: tuple[int, ...] = ()
    if planned and not dry_run:
        with transaction.atomic():
            region = Region.objects.active().select_for_update().get(pk=region.pk)
            ProviderAccount.objects.select_for_update().get(pk=target.provider_account_id)
            source, target = _bindings(region, source.pk, target.pk, lock=True)
            _checked_factor(source, target)
            copied = dict(
                copies.filter(source_snapshot__in=[copy.source.pk for copy in planned]).values_list(
                    "source_snapshot_id", "snapshot_id"
                )
            )
            reused += tuple(copied.values())
            planned = [copy for copy in planned if copy.source.pk not in copied]
            created = _write(planned, target, actor, reference, factor, moment)
            if created:
                record_audit(
                    actor=actor,
                    region=region,
                    action=AuditAction.REFERENCE_CAPACITY_NORMALIZED,
                    revision=region.revision,
                )
    dates = sorted(copy.job.target_date for copy in planned)
    return NormalizationOutcome(
        source_binding_id=source.pk,
        target_binding_id=target.pk,
        source_capacity_kw=source.capacity_kw,
        target_capacity_kw=target.capacity_kw,
        factor=factor,
        created=created,
        reused=reused,
        by_endpoint=dict(Counter(copy.source.job.endpoint for copy in planned)),
        first_target_date=dates[0] if dates else None,
        last_target_date=dates[-1] if dates else None,
        predecessors=tuple(
            ReferenceCapacityNormalization.objects.filter(target_binding=source)
            .values_list("source_snapshot__job__binding_id", flat=True)
            .distinct()
            .order_by("source_snapshot__job__binding_id")
        ),
        dry_run=dry_run,
    )
