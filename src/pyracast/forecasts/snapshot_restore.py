import gzip
import hashlib
import json
import zlib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
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
from pyracast.connections.models import Provider, ProviderAccount, SiteBinding
from pyracast.forecasts.actuals import select_archived_actual_source
from pyracast.forecasts.coverage import intervals_cover_day
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobError,
    CollectionJobStatus,
    ForecastResult,
    ForecastSnapshot,
    SnapshotSourceKind,
)
from pyracast.forecasts.projections import prepare_daily_payloads
from pyracast.forecasts.sources import snapshot_data
from pyracast.forecasts.timing import approval_job
from pyracast.forecasts.trust import reference_problems
from pyracast.plants.preparation import request_region_preparation
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
# Record-only copies of the provider's site settings: no calculation reads them.
RECORDED_SETTINGS = ("installation_date",)
ACCEPTED_KEY = "accepted_differences"
SOURCE_LABELS: dict[str, str] = {
    CollectionEndpoint.ESTIMATED_ACTUALS: "native",
    CollectionEndpoint.HISTORICAL_IMPORT: "historical import",
    CollectionEndpoint.SCALED_ESTIMATED_ACTUALS: "scaled copy",
}
JOB_KEY = "job"
_MISSING = object()


class RestoreRefusedError(ValidationError):
    # Its message is a fixed sentence naming only lines and fields; the command prints it.
    pass


class Outcome(StrEnum):
    RESTORED = "restored"
    ALREADY_RESTORED = "already_restored"
    OPEN_AT_APPROVAL = "open_at_approval"
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
class ExportedJob:
    scheduled_for: datetime
    target_date: date
    available_at: datetime
    retry_until: datetime | None
    attempts: int
    last_error: str


@dataclass(frozen=True)
class Acquisition:
    row: ExportRow
    collected_at: datetime
    available_at: datetime
    issued_at: datetime | None
    coverage: tuple[datetime, datetime] | None
    raw_payload: dict[str, Any]
    intervals: list[Any]
    weather_payload: dict[str, Any] | None
    job: ExportedJob


@dataclass(frozen=True)
class EndpointSummary:
    endpoint: str
    outcomes: tuple[tuple[Outcome, int], ...]
    period: tuple[datetime, datetime] | None


@dataclass(frozen=True)
class AcceptedDifference:
    # Values are JSON text: a string keeps its quotes.
    name: str
    exported: tuple[str, ...]
    binding: str


@dataclass(frozen=True)
class RestoreSummary:
    binding_id: int
    export_sha256: str
    rows: int
    dry_run: bool
    timezone: str
    endpoints: tuple[EndpointSummary, ...]
    days: tuple[tuple[date, str], ...]
    accepted: tuple[AcceptedDifference, ...]

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
        raise RestoreRefusedError(f"Export line {line} is not a provider snapshot.") from error
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
            raise RestoreRefusedError("The export is not readable gzip.") from error
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


def _expected(expected: object, value: object) -> object:
    if not _same(value, expected):
        raise ValueError("A succeeded live job holds another value")
    return value


def _slot(collected_at: datetime, value: object) -> datetime:
    slot = _instant(value)
    if slot > collected_at:
        raise ValueError("A job collects at or after its slot")
    return slot


def _day(value: object) -> date:
    if not isinstance(value, str):
        raise TypeError("An ISO date is required")
    return date.fromisoformat(value)


def _attempts(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("A count of attempts is required")
    return value


def _job_error(value: object) -> str:
    if not isinstance(value, str) or (value and value not in CollectionJobError.values):
        raise ValueError("A collection job error is required")
    return value


def _job_value[T](row: ExportRow, job: dict[str, Any], name: str, parse: Callable[[Any], T]) -> T:
    try:
        return parse(job[name])
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise RestoreRefusedError(
            f"Export line {row.line} does not hold a succeeded live job: {JOB_KEY}.{name}."
        ) from error


def _exported_job(row: ExportRow, collected_at: datetime) -> ExportedJob:
    # Id, binding and creation time belong to the erased database and are not taken.
    job = row.values.get(JOB_KEY)
    if not isinstance(job, dict):
        raise RestoreRefusedError(
            f"Export line {row.line} does not hold a succeeded live job: {JOB_KEY}."
        )
    for name, expected in (
        ("endpoint", row.endpoint),
        ("status", CollectionJobStatus.SUCCEEDED),
        ("reference_import_id", None),
        ("lease_token", None),
        ("lease_expires_at", None),
    ):
        _job_value(row, job, name, partial(_expected, expected))
    return ExportedJob(
        _job_value(row, job, "scheduled_for", partial(_slot, collected_at)),
        _job_value(row, job, "target_date", _day),
        _job_value(row, job, "available_at", _instant),
        _job_value(row, job, "retry_until", _optional_instant),
        _job_value(row, job, "attempts", _attempts),
        _job_value(row, job, "last_error", _job_error),
    )


def _acquisition(row: ExportRow, now: datetime) -> Acquisition:
    values = row.values
    try:
        collected_at = _instant(values["collected_at"])
        # Publication sets a succeeded job's snapshot availability (jobs.publish).
        available_at = _instant(values["available_at"])
        issued_at = _optional_instant(values["issued_at"])
        coverage = _coverage(values["coverage_start"], values["coverage_end"])
        raw_payload, intervals = values["raw_payload"], values["normalized_intervals"]
        weather_payload = values["weather_payload"]
        if (
            row.source_kind != LIVE_KINDS[row.endpoint]
            or collected_at > now
            or not isinstance(raw_payload, dict)
            or not isinstance(intervals, list)
            or not all(_valid_interval(interval) for interval in intervals)
            or not isinstance(weather_payload, dict | None)
        ):
            raise ValueError("The row is not a restorable live acquisition")
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise RestoreRefusedError(
            f"Export line {row.line} does not hold a restorable live acquisition."
        ) from error
    return Acquisition(
        row,
        collected_at,
        available_at,
        issued_at,
        coverage,
        raw_payload,
        intervals,
        weather_payload,
        _exported_job(row, collected_at),
    )


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


def _accepted_differences(
    binding: SiteBinding, rows: list[ExportRow], accepted: frozenset[str]
) -> tuple[AcceptedDifference, ...]:
    differing = _differing_settings(binding, rows)
    refused = [
        name
        for name in differing
        if name not in accepted or any(name not in row.binding for row in rows)
    ]
    if refused:
        raise RestoreRefusedError(
            f"The export's provider site differs from the binding in: {', '.join(refused)}."
        )
    unused = sorted(accepted.difference(differing))
    if unused:
        raise RestoreRefusedError(
            f"The export's provider site does not differ from the binding in: {', '.join(unused)}."
        )
    return tuple(
        AcceptedDifference(
            name,
            tuple(sorted({json.dumps(row.binding[name]) for row in rows})),
            json.dumps(_setting(binding, name)),
        )
        for name in differing
    )


def _publish_forecast(
    snapshot: ForecastSnapshot, binding: SiteBinding, line: int, published_at: datetime
) -> None:
    try:
        data = snapshot_data(snapshot)
        payload = calculate(binding, snapshot.job.target_date, data)
    except (ValueError, ArithmeticError) as error:
        raise RestoreRefusedError(
            f"Export line {line}: the forecast cannot be calculated."
        ) from error
    ForecastResult.objects.create(
        snapshot=snapshot,
        payload=payload,
        daily_payloads=prepare_daily_payloads(
            binding, snapshot.job.target_date, data, calculate, payload
        ),
        published_at=published_at,
    )


def _store(
    acquisition: Acquisition,
    binding: SiteBinding,
    provenance: dict[str, object],
    accepted: tuple[AcceptedDifference, ...],
) -> None:
    row, exported = acquisition.row, acquisition.job
    job = CollectionJob.objects.create(
        binding=binding,
        endpoint=row.endpoint,
        scheduled_for=exported.scheduled_for,
        target_date=exported.target_date,
        status=CollectionJobStatus.SUCCEEDED,
        available_at=exported.available_at,
        retry_until=exported.retry_until,
        attempts=exported.attempts,
        last_error=exported.last_error,
    )
    coverage_start, coverage_end = acquisition.coverage or (None, None)
    record = {**provenance, "snapshot_id": row.snapshot_id}
    if accepted:
        record[ACCEPTED_KEY] = {
            item.name: {
                "export": row.binding[item.name],
                "binding": _setting(binding, item.name),
            }
            for item in accepted
        }
    if PROVENANCE_KEY in acquisition.raw_payload:
        record["previous"] = acquisition.raw_payload[PROVENANCE_KEY]
    snapshot = ForecastSnapshot.objects.create(
        job=job,
        binding=binding,
        raw_payload={**acquisition.raw_payload, PROVENANCE_KEY: record},
        normalized_intervals=acquisition.intervals,
        collected_at=acquisition.collected_at,
        issued_at=acquisition.issued_at,
        source_kind=row.source_kind,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        available_at=acquisition.available_at,
        weather_payload=acquisition.weather_payload,
    )
    if row.endpoint == CollectionEndpoint.FORECASTS:
        # A live forecast's result and its snapshot are published at the same moment.
        _publish_forecast(snapshot, binding, row.line, acquisition.available_at)


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
    binding: SiteBinding,
    rows: tuple[ExportRow, ...],
    digest: str,
    moment: datetime,
    *,
    dry_run: bool,
    accepted: frozenset[str],
) -> RestoreSummary:
    outcomes: defaultdict[str, Counter[Outcome]] = defaultdict(Counter)
    matching = []
    for row in rows:
        skipped = _skipped(row, binding)
        if skipped is None:
            matching.append(row)
        else:
            outcomes[row.endpoint][skipped] += 1
    differences = _accepted_differences(binding, matching, accepted)
    acquisitions = sorted(
        (_acquisition(row, moment) for row in matching),
        key=lambda item: (item.collected_at, item.row.snapshot_id),
    )
    # The binding's own schedule starts at its approval collection; restored ones lie before.
    _, approval_slot = approval_job(binding.pk) or (None, None)
    if approval_slot is None:
        raise RestoreRefusedError("The binding holds no approval collection.")
    later = min(
        (item.row.line for item in acquisitions if item.job.scheduled_for >= approval_slot),
        default=None,
    )
    if later is not None:
        raise RestoreRefusedError(
            f"Export line {later} is not before the binding's approval collection: "
            f"{JOB_KEY}.scheduled_for."
        )
    # An acquisition is its endpoint and job slot, the binding's unique collection slot;
    # snapshot ids restart with each database.
    slots = [item.job.scheduled_for for item in acquisitions]
    restored = set(
        ForecastSnapshot.objects.filter(
            binding=binding,
            job__endpoint__in=tuple(LIVE_KINDS),
            job__scheduled_for__in=slots,
            raw_payload__has_key=PROVENANCE_KEY,
        ).values_list("job__endpoint", "job__scheduled_for")
    )
    taken = set(
        CollectionJob.objects.filter(
            binding=binding, endpoint__in=tuple(LIVE_KINDS), scheduled_for__in=slots
        ).values_list("endpoint", "scheduled_for")
    )
    provenance: dict[str, object] = {
        "export_sha256": digest,
        "restored_by_id": actor.pk,
        "restored_at": moment.isoformat(),
    }
    for item in acquisitions:
        key = (item.row.endpoint, item.job.scheduled_for)
        # A retry window still open at approval belongs to the binding's own schedule: the
        # scheduler's nightly lookup would take a restored job of it for tonight's collection.
        if item.job.retry_until is not None and item.job.retry_until > approval_slot:
            outcome = Outcome.OPEN_AT_APPROVAL
        elif key in restored:
            outcome = Outcome.ALREADY_RESTORED
        elif key in taken:
            outcome = Outcome.SLOT_TAKEN
        else:
            _store(item, binding, provenance, differences)
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
        differences,
    )


def restore_provider_snapshots(
    actor: User,
    tenant_id: int,
    region_id: int,
    binding_id: int,
    content: bytes,
    *,
    dry_run: bool = False,
    accepted: Iterable[str] = (),
) -> RestoreSummary:
    """Store the live collections of a reset export (`deploy/reset_database.py`) as native
    collections of the binding that now holds the same provider site; a dry run rolls back."""
    require_platform_admin(actor)
    names = frozenset(accepted)
    unrecorded = sorted(names.difference(RECORDED_SETTINGS))
    if unrecorded:
        raise RestoreRefusedError(
            f"Only a recorded setting can be accepted, not: {', '.join(unrecorded)}."
        )
    moment = timezone.now()
    rows = _read_export(content)
    with transaction.atomic():
        region = Region.objects.active().select_for_update().get(pk=region_id, tenant_id=tenant_id)
        binding = SiteBinding.objects.get(pk=binding_id, region=region)
        account = ProviderAccount.objects.select_for_update().get(pk=binding.provider_account_id)
        binding = SiteBinding.objects.select_for_update().get(pk=binding.pk)
        binding.provider_account = account
        binding.region = region
        if reference_problems(binding):
            raise RestoreRefusedError(
                "A current authorized and validated Solcast binding is required."
            )
        summary = _restore_rows(
            actor,
            binding,
            rows,
            hashlib.sha256(content).hexdigest(),
            moment,
            dry_run=dry_run,
            accepted=names,
        )
        if dry_run:
            transaction.set_rollback(True)
        elif any(
            item.endpoint == CollectionEndpoint.ESTIMATED_ACTUALS
            and Outcome.RESTORED in dict(item.outcomes)
            for item in summary.endpoints
        ):
            request_region_preparation(region.pk)
    return summary
