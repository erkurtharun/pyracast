import gzip
import hashlib
import json
import zlib
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from functools import partial
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from pyracast.accounts.authorization import require_platform_admin
from pyracast.calculation import calculate
from pyracast.collection_window import INTRADAY_HISTORY_WINDOW, nightly_window
from pyracast.connections.models import Provider, ProviderAccount, SiteBinding
from pyracast.forecasts.actuals import select_archived_actual_source
from pyracast.forecasts.coverage import intervals_cover_day
from pyracast.forecasts.historical_import import (
    _prepare_region_calibrations,
    _validate_authorized_binding,
)
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobStatus,
    ForecastResult,
    ForecastSnapshot,
    SnapshotSourceKind,
)
from pyracast.forecasts.projections import prepare_daily_payloads
from pyracast.forecasts.sources import snapshot_data
from pyracast.regions.models import Region

# Sibling of the provider's keys in raw_payload; no reader depends on it. A row that was
# restored before keeps its earlier record under "previous".
PROVENANCE_KEY = "snapshot_restore"
LIVE_KINDS: dict[str, str] = {
    CollectionEndpoint.FORECASTS: SnapshotSourceKind.FORECAST,
    CollectionEndpoint.ESTIMATED_ACTUALS: SnapshotSourceKind.ESTIMATED_ACTUALS,
}
# The provider site and the binding columns the stored numbers depend on: the provider's
# capacity scale and AC/DC semantics, the geometry, losses and installation date of its site
# model, and the location and calendar the readers use for daylight and local days.
SETTINGS = (
    "site_id",
    "capacity_kw",
    "dc_capacity_kw",
    "power_basis",
    "capacity_semantics_verified",
    "azimuth_degrees",
    "losses_percent",
    "installation_date",
    "latitude",
    "longitude",
    "timezone",
)
SOURCE_LABELS: dict[str, str] = {
    CollectionEndpoint.ESTIMATED_ACTUALS: "native",
    CollectionEndpoint.HISTORICAL_IMPORT: "historical import",
    CollectionEndpoint.SCALED_ESTIMATED_ACTUALS: "scaled copy",
}
_MISSING = object()


class Outcome(StrEnum):
    RESTORED = "restored"
    ALREADY_RESTORED = "already_restored"
    SLOT_TAKEN = "slot_taken"
    OTHER_PROVIDER = "other_provider"
    NOT_LIVE = "not_live"


@dataclass(frozen=True)
class ExportRow:
    line: int
    snapshot_id: int
    provider: str
    endpoint: str
    source_kind: str
    binding: dict[str, Any]
    values: dict[str, Any]


@dataclass(frozen=True)
class Acquisition:
    row: ExportRow
    collected_at: datetime
    issued_at: datetime | None
    coverage: tuple[datetime, datetime] | None
    raw_payload: dict[str, Any]
    intervals: list[Any]
    weather_payload: dict[str, Any] | None


@dataclass(frozen=True)
class EndpointSummary:
    endpoint: str
    outcomes: tuple[tuple[Outcome, int], ...]
    period: tuple[datetime, datetime] | None


@dataclass(frozen=True)
class RestoreSummary:
    binding_id: int
    export_sha256: str
    rows: int
    dry_run: bool
    timezone: str
    endpoints: tuple[EndpointSummary, ...]
    days: tuple[tuple[date, str], ...]

    @property
    def restored(self) -> int:
        return sum(
            count
            for item in self.endpoints
            for outcome, count in item.outcomes
            if outcome == Outcome.RESTORED
        )


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not a JSON number")


def _row(line: int, text: bytes) -> ExportRow:
    try:
        values = json.loads(text, parse_constant=_reject_constant)
        identifier, site = values["id"], values["binding"]
        if (
            type(identifier) is not int
            or identifier <= 0
            or values["provider"] not in Provider.values
            or values["endpoint"] not in CollectionEndpoint.values
            or values["source_kind"] not in SnapshotSourceKind.values
            or not isinstance(site, dict)
            or not isinstance(site.get("site_id"), str)
        ):
            raise ValueError("The line does not identify a provider snapshot")
    except (KeyError, TypeError, ValueError) as error:
        raise ValidationError(f"Export line {line} is not a provider snapshot.") from error
    return ExportRow(
        line,
        identifier,
        values["provider"],
        values["endpoint"],
        values["source_kind"],
        site,
        values,
    )


def _read_export(content: bytes) -> tuple[ExportRow, ...]:
    if content.startswith(b"\x1f\x8b"):
        try:
            content = gzip.decompress(content)
        except (OSError, EOFError, zlib.error) as error:
            raise ValidationError("The export is not readable gzip.") from error
    # JSON text may hold U+2028 and other line separators, so only a newline ends a row.
    return tuple(
        _row(line, text) for line, text in enumerate(content.split(b"\n"), start=1) if text.strip()
    )


def _instant(value: object) -> datetime:
    moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    if moment is None or moment.utcoffset() is None:
        raise ValueError("An ISO instant with its offset is required")
    return moment


def _optional_instant(value: object) -> datetime | None:
    return None if value is None else _instant(value)


def _coverage(start: object, end: object) -> tuple[datetime, datetime] | None:
    if start is None and end is None:
        return None
    left, right = _instant(start), _instant(end)
    if right <= left:
        raise ValueError("Coverage must end after it starts")
    return left, right


def _valid_interval(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    power = row.get("power_kw")
    return _instant(row.get("end")) > _instant(row.get("start")) and (
        power is None
        or (not isinstance(power, bool) and isinstance(power, int | float) and isfinite(power))
    )


def _acquisition(row: ExportRow, now: datetime) -> Acquisition:
    values = row.values
    try:
        acquisition = Acquisition(
            row,
            _instant(values["collected_at"]),
            _optional_instant(values["issued_at"]),
            _coverage(values["coverage_start"], values["coverage_end"]),
            values["raw_payload"],
            values["normalized_intervals"],
            values["weather_payload"],
        )
        if (
            row.source_kind != LIVE_KINDS[row.endpoint]
            or acquisition.collected_at > now
            or not isinstance(acquisition.raw_payload, dict)
            or not isinstance(acquisition.intervals, list)
            or not all(_valid_interval(interval) for interval in acquisition.intervals)
            or not isinstance(acquisition.weather_payload, dict | None)
        ):
            raise ValueError("The row is not a restorable live acquisition")
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValidationError(
            f"Export line {row.line} does not hold a restorable live acquisition."
        ) from error
    return acquisition


def _skipped(row: ExportRow, binding: SiteBinding) -> Outcome | None:
    if row.endpoint not in LIVE_KINDS:
        return Outcome.NOT_LIVE
    if row.provider != binding.provider_account.provider:
        return Outcome.OTHER_PROVIDER
    return None


def _setting(binding: SiteBinding, name: str) -> object:
    value = getattr(binding, name)
    return value.isoformat() if isinstance(value, date) else value


def _same(exported: object, current: object) -> bool:
    return exported == current and isinstance(exported, bool) == isinstance(current, bool)


def _differing_settings(binding: SiteBinding, rows: Iterable[ExportRow]) -> list[str]:
    return sorted(
        {
            name
            for row in rows
            for name in SETTINGS
            if not _same(row.binding.get(name, _MISSING), _setting(binding, name))
        }
    )


def _job_schedule(endpoint: str, slot: datetime, zone: str) -> tuple[date, datetime | None]:
    # The export carries no job slot, so the collection time is the slot. From it the rules of
    # forecasts/scheduler.py give a forecast the next local day, a collection inside the nightly
    # window the previous day until the window closes, and any other its own day.
    day = slot.astimezone(ZoneInfo(zone)).date()
    if endpoint == CollectionEndpoint.FORECASTS:
        return day + timedelta(days=1), None
    night = nightly_window(day, zone)
    if night is not None and night[0] <= slot < night[1]:
        return day - timedelta(days=1), night[1]
    return day, slot + INTRADAY_HISTORY_WINDOW


def _publish_forecast(snapshot: ForecastSnapshot, binding: SiteBinding, line: int) -> None:
    data = snapshot_data(snapshot)
    try:
        payload = calculate(binding, snapshot.job.target_date, data)
    except (ValueError, ArithmeticError) as error:
        raise ValidationError(f"Export line {line}: the forecast cannot be calculated.") from error
    ForecastResult.objects.create(
        snapshot=snapshot,
        payload=payload,
        daily_payloads=prepare_daily_payloads(
            binding, snapshot.job.target_date, data, calculate, payload
        ),
        published_at=snapshot.collected_at,
    )


def _store(
    acquisition: Acquisition, binding: SiteBinding, zone: str, provenance: dict[str, object]
) -> None:
    row, slot = acquisition.row, acquisition.collected_at
    target_date, retry_until = _job_schedule(row.endpoint, slot, zone)
    job = CollectionJob.objects.create(
        binding=binding,
        endpoint=row.endpoint,
        scheduled_slot=slot,
        target_date=target_date,
        status=CollectionJobStatus.SUCCEEDED,
        available_at=slot,
        retry_until=retry_until,
        attempts=1,
    )
    coverage_start, coverage_end = acquisition.coverage or (None, None)
    record = {**provenance, "snapshot_id": row.snapshot_id}
    if PROVENANCE_KEY in acquisition.raw_payload:
        record["previous"] = acquisition.raw_payload[PROVENANCE_KEY]
    snapshot = ForecastSnapshot.objects.create(
        job=job,
        binding=binding,
        raw_payload={**acquisition.raw_payload, PROVENANCE_KEY: record},
        normalized_intervals=acquisition.intervals,
        collected_at=slot,
        issued_at=acquisition.issued_at,
        source_kind=row.source_kind,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        available_at=slot,
        weather_payload=acquisition.weather_payload,
    )
    if row.endpoint == CollectionEndpoint.FORECASTS:
        _publish_forecast(snapshot, binding, row.line)


def _period(
    coverages: Iterable[tuple[datetime, datetime] | None],
) -> tuple[datetime, datetime] | None:
    covered = [coverage for coverage in coverages if coverage is not None]
    if not covered:
        return None
    return min(start for start, _ in covered), max(end for _, end in covered)


def _day_state(binding: SiteBinding, day: date, moment: datetime) -> str:
    source = select_archived_actual_source(binding, day, moment)
    if source is None:
        return "missing"
    endpoint = source.snapshot.job.endpoint
    complete = intervals_cover_day(source.intervals, day, binding.timezone)
    return f"{SOURCE_LABELS.get(endpoint, endpoint)} {'complete' if complete else 'partial'}"


def _day_states(
    binding: SiteBinding, period: tuple[datetime, datetime] | None, moment: datetime
) -> tuple[tuple[date, str], ...]:
    if period is None:
        return ()
    zone = ZoneInfo(binding.timezone)
    day = period[0].astimezone(zone).date()
    last = (period[1] - timedelta(microseconds=1)).astimezone(zone).date()
    states = []
    while day <= last:
        states.append((day, _day_state(binding, day, moment)))
        day += timedelta(days=1)
    return tuple(states)


def _restore_rows(
    actor: User,
    region: Region,
    binding: SiteBinding,
    rows: tuple[ExportRow, ...],
    digest: str,
    moment: datetime,
    *,
    dry_run: bool,
) -> RestoreSummary:
    outcomes: defaultdict[str, Counter[Outcome]] = defaultdict(Counter)
    matching = []
    for row in rows:
        skipped = _skipped(row, binding)
        if skipped is None:
            matching.append(row)
        else:
            outcomes[row.endpoint][skipped] += 1
    differing = _differing_settings(binding, matching)
    if differing:
        raise ValidationError(
            f"The export's provider site differs from the binding in: {', '.join(differing)}."
        )
    acquisitions = sorted(
        (_acquisition(row, moment) for row in matching),
        key=lambda item: (item.collected_at, item.row.snapshot_id),
    )
    # An acquisition is its endpoint and collection time: snapshot ids restart with each
    # database, and a restored job's slot is its collection time.
    times = [item.collected_at for item in acquisitions]
    held = ForecastSnapshot.objects.filter(
        binding=binding, job__endpoint__in=tuple(LIVE_KINDS), collected_at__in=times
    )
    restored = set(
        held.filter(raw_payload__has_key=PROVENANCE_KEY).values_list(
            "job__endpoint", "collected_at"
        )
    )
    taken = set(held.values_list("job__endpoint", "collected_at")) | set(
        CollectionJob.objects.filter(
            binding=binding, endpoint__in=tuple(LIVE_KINDS), scheduled_slot__in=times
        ).values_list("endpoint", "scheduled_slot")
    )
    provenance: dict[str, object] = {
        "export_sha256": digest,
        "restored_by_id": actor.pk,
        "restored_at": moment.isoformat(),
    }
    for item in acquisitions:
        key = (item.row.endpoint, item.collected_at)
        if key in restored:
            outcome = Outcome.ALREADY_RESTORED
        elif key in taken:
            outcome = Outcome.SLOT_TAKEN
        else:
            _store(item, binding, region.timezone, provenance)
            restored.add(key)
            taken.add(key)
            outcome = Outcome.RESTORED
        outcomes[item.row.endpoint][outcome] += 1
    periods = {
        endpoint: _period(item.coverage for item in acquisitions if item.row.endpoint == endpoint)
        for endpoint in LIVE_KINDS
    }
    return RestoreSummary(
        binding.pk,
        digest,
        len(rows),
        dry_run,
        binding.timezone,
        tuple(
            EndpointSummary(
                endpoint,
                tuple((outcome, counts[outcome]) for outcome in Outcome if counts[outcome]),
                periods.get(endpoint),
            )
            for endpoint, counts in sorted(outcomes.items())
        ),
        _day_states(binding, periods[CollectionEndpoint.ESTIMATED_ACTUALS], moment),
    )


def restore_provider_snapshots(
    actor: User,
    tenant_id: int,
    region_id: int,
    binding_id: int,
    content: bytes,
    *,
    dry_run: bool = False,
) -> RestoreSummary:
    """Store the live collections of a reset export (`deploy/reset_database.py`) as native
    collections of the binding that now holds the same provider site; a dry run rolls back."""
    require_platform_admin(actor)
    moment = timezone.now()
    rows = _read_export(content)
    with transaction.atomic():
        region = Region.objects.select_for_update().get(
            pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True
        )
        binding = SiteBinding.objects.get(pk=binding_id, region=region)
        account = ProviderAccount.objects.select_for_update().get(pk=binding.provider_account_id)
        binding = SiteBinding.objects.select_for_update().get(pk=binding.pk)
        binding.provider_account = account
        _validate_authorized_binding(binding)
        summary = _restore_rows(
            actor,
            region,
            binding,
            rows,
            hashlib.sha256(content).hexdigest(),
            moment,
            dry_run=dry_run,
        )
        if dry_run:
            transaction.set_rollback(True)
        elif any(
            item.endpoint == CollectionEndpoint.ESTIMATED_ACTUALS
            and Outcome.RESTORED in dict(item.outcomes)
            for item in summary.endpoints
        ):
            transaction.on_commit(partial(_prepare_region_calibrations, region.pk))
    return summary
