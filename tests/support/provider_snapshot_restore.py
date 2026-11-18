import gzip
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.db import connection

from deploy.reset_database import SOLCAST_EXPORT_SQL
from pyracast.accounts.models import Tenant
from pyracast.collection_window import INTRADAY_HISTORY_WINDOW
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastSnapshot,
    HistoricalReferenceImport,
)
from pyracast.regions.models import Region
from tests.support.domain import NOW, System
from tests.support.historical_reference_import import archive, power_file, system_for_import
from tests.support.plant_operations import half_hours

SITE = "SITE-7Q3X"
OTHER_SITE = "SITE-OTHER-2"
ZONE = "Europe/Istanbul"
# A live job collects a few seconds after its scheduled slot.
COLLECTION_DELAY = timedelta(seconds=4)
# The job columns a restored job takes from the exported job row.
JOB_FIELDS = (
    "endpoint",
    "scheduled_for",
    "target_date",
    "status",
    "available_at",
    "retry_until",
    "attempts",
    "last_error",
    "reference_import_id",
    "lease_token",
    "lease_expires_at",
)
# The export writes JSON text unescaped, so a payload can hold this line separator.
SEPARATOR = chr(0x2028)
# The binding columns that give stored provider numbers their meaning, as the restore compares them.
SETTINGS = (
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


def local(day: date, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(
        day.year, day.month, day.day, hour, minute, second, tzinfo=ZoneInfo(ZONE)
    ).astimezone(UTC)


def job_fields(job: CollectionJob) -> dict[str, object]:
    return {name: getattr(job, name) for name in JOB_FIELDS}


def restore_system(monkeypatch: pytest.MonkeyPatch) -> System:
    system = system_for_import(monkeypatch)
    SiteBinding.objects.filter(pk=system.binding.pk).update(site_id=SITE)
    system.binding.refresh_from_db()
    return system


def binding_before_reset(system: System, *, site_id: str = SITE) -> SiteBinding:
    tenant = Tenant.objects.create(name="Before the reset")
    region = Region.objects.create(
        tenant=tenant, name="Plant", latitude=40, longitude=29, timezone=ZONE
    )
    target = system.binding
    return SiteBinding.objects.create(
        region=region,
        region_revision=1,
        provider_account=system.account,
        site_id=site_id,
        settings_verification_reference="Verified before the reset",
        **{name: getattr(target, name) for name in SETTINGS},
    )


def acquisition(
    binding: SiteBinding,
    endpoint: str,
    slot: datetime,
    start: datetime,
    end: datetime,
    *,
    collected_at: datetime | None = None,
    target_date: date | None = None,
    retry_until: datetime | None = None,
    available_at: datetime | None = None,
    attempts: int = 1,
    last_error: str = "",
    rows: list[dict[str, Any]] | None = None,
    weather_payload: dict[str, Any] | None = None,
    note: str | None = None,
) -> ForecastSnapshot:
    """A succeeded job at `slot` and its snapshot; without explicit values the job takes
    the scheduler's shape: a forecast targets the next local day, and estimated actuals
    are an intraday collection of the slot's day bounded by the intraday window."""
    collected_at = slot + COLLECTION_DELAY if collected_at is None else collected_at
    rows = half_hours(start, end, 0.5) if rows is None else rows
    kind = "forecast" if endpoint.endswith("forecasts") else "estimated_actuals"
    day = slot.astimezone(ZoneInfo(ZONE)).date()
    live = endpoint in ("forecasts", "estimated_actuals")
    if target_date is None:
        target_date = (
            day + timedelta(days=1)
            if endpoint == "forecasts"
            else day
            if live
            else start.astimezone(ZoneInfo(ZONE)).date()
        )
    if retry_until is None and endpoint == "estimated_actuals":
        retry_until = slot + INTRADAY_HISTORY_WINDOW
    payload: dict[str, Any] = {
        "forecasts" if kind == "forecast" else "estimated_actuals": [
            {"period_end": row["end"], "period": "PT30M", "pv_estimate": row["power_kw"]}
            for row in rows
        ]
    }
    if note is not None:
        payload["note"] = note
    reference = (
        HistoricalReferenceImport.objects.create(
            binding=binding,
            content_sha256="0" * 64,
            source_name="before-reset.json",
            source_settings={},
            imported_by=User.objects.get(username="admin"),
            created_at=collected_at,
            interval_count=len(rows),
        )
        if endpoint == "historical_import"
        else None
    )
    job = CollectionJob.objects.create(
        binding=binding,
        reference_import=reference,
        endpoint=endpoint,
        scheduled_for=slot,
        target_date=target_date,
        status="succeeded",
        available_at=slot if available_at is None else available_at,
        retry_until=retry_until,
        attempts=attempts,
        last_error=last_error,
    )
    return ForecastSnapshot.objects.create(
        job=job,
        binding=binding,
        raw_payload=payload,
        normalized_intervals=rows,
        collected_at=collected_at,
        issued_at=None,
        source_kind=kind,
        coverage_start=start,
        coverage_end=end,
        available_at=collected_at + timedelta(minutes=1),
        weather_payload=weather_payload,
    )


def export_content(*, compress: bool = True) -> bytes:
    with connection.cursor() as cursor, cursor.cursor.copy(SOLCAST_EXPORT_SQL) as copy:
        lines = b"".join(copy)
    return gzip.compress(lines) if compress else lines


def export_rows(content: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in gzip.decompress(content).split(b"\n") if line]


def encode_rows(rows: list[dict[str, Any]]) -> bytes:
    return gzip.compress(b"".join(json.dumps(row).encode() + b"\n" for row in rows))


@dataclass(frozen=True)
class BeforeReset:
    approval: ForecastSnapshot
    lookback: ForecastSnapshot
    intraday: ForecastSnapshot
    nights: tuple[ForecastSnapshot, ...]
    forecasts: tuple[ForecastSnapshot, ...]
    content: bytes

    @property
    def actuals(self) -> tuple[ForecastSnapshot, ...]:
        return (self.lookback, self.intraday, *self.nights)

    @property
    def live(self) -> tuple[ForecastSnapshot, ...]:
        return (*self.actuals, self.approval, *self.forecasts)


def _nightly(binding: SiteBinding, day: date, start: datetime, **job: Any) -> ForecastSnapshot:
    """The nightly history job of local `day`: slot at the window start, the previous day
    as target and the window end as retry bound."""
    return acquisition(
        binding,
        "estimated_actuals",
        local(day),
        start,
        local(day),
        target_date=day - timedelta(days=1),
        retry_until=local(day, 9),
        **job,
    )


def collections_before_reset(system: System) -> BeforeReset:
    """What the erased binding collected (clocks 08:00 and 10:00 local, approved on
    4 November at 14:23:17 local) beside an import and a scaled copy, exported by the
    reset tool: the approval forecast, forecasts at clocks, a seven-day lookback at 10:00,
    an intraday collection at 08:00 and three nightly collections, one after a retry."""
    before = binding_before_reset(system)
    approval = acquisition(
        before,
        "forecasts",
        local(date(2026, 11, 4), 14, 23, 17),
        datetime(2026, 11, 4, 21, tzinfo=UTC),
        datetime(2026, 11, 7, 21, tzinfo=UTC),
        collected_at=local(date(2026, 11, 4), 14, 23, 19),
    )
    lookback_start = datetime(2026, 10, 29, 7, tzinfo=UTC)
    lookback_end = local(date(2026, 11, 5), 10)
    rows = half_hours(lookback_start, lookback_end, 0.5)
    rows[-1]["power_kw"] = None
    lookback = acquisition(
        before,
        "estimated_actuals",
        lookback_end,
        lookback_start,
        lookback_end,
        available_at=lookback_end + timedelta(minutes=5, seconds=1),
        rows=rows,
        note="line" + SEPARATOR + "separator",
    )
    intraday = acquisition(
        before,
        "estimated_actuals",
        local(date(2026, 11, 7), 8),
        local(date(2026, 11, 7)),
        local(date(2026, 11, 7), 8),
        available_at=local(date(2026, 11, 7), 8, 5, 1),
    )
    nights = (
        _nightly(
            before,
            date(2026, 11, 6),
            lookback_end,
            available_at=local(date(2026, 11, 6), 0, 30, 1),
        ),
        _nightly(
            before,
            date(2026, 11, 7),
            local(date(2026, 11, 6)),
            collected_at=local(date(2026, 11, 7), 0, 30, 4),
            available_at=local(date(2026, 11, 7), 1, 0, 2),
            attempts=2,
            last_error="history_incomplete",
        ),
        _nightly(
            before,
            date(2026, 11, 8),
            local(date(2026, 11, 7)),
            available_at=local(date(2026, 11, 8), 0, 30, 1),
        ),
    )
    forecasts = (
        acquisition(
            before,
            "forecasts",
            local(date(2026, 11, 5), 8),
            datetime(2026, 11, 4, 21, tzinfo=UTC),
            datetime(2026, 11, 7, 21, tzinfo=UTC),
            weather_payload={"source": "open-meteo"},
        ),
        acquisition(
            before,
            "forecasts",
            local(date(2026, 11, 6), 10),
            datetime(2026, 11, 5, 21, tzinfo=UTC),
            datetime(2026, 11, 8, 21, tzinfo=UTC),
        ),
    )
    acquisition(
        before,
        "historical_import",
        datetime(2026, 11, 4, 9, tzinfo=UTC),
        local(date(2026, 11, 3)),
        local(date(2026, 11, 4)),
    )
    acquisition(
        before,
        "scaled_estimated_actuals",
        datetime(2026, 11, 6, 9, tzinfo=UTC),
        local(date(2026, 11, 5)),
        local(date(2026, 11, 6)),
    )
    return BeforeReset(approval, lookback, intraday, nights, forecasts, export_content())


def replacement_binding(system: System, *, approved_at: datetime = NOW) -> SiteBinding:
    region = Region.objects.get(pk=system.region.pk)
    target = system.binding
    fresh = SiteBinding.objects.create(
        region=region,
        region_revision=region.revision + 1,
        provider_account=system.account,
        site_id=target.site_id,
        settings_verification_reference=target.settings_verification_reference,
        validated_at=target.validated_at,
        validated_credential_revision=target.validated_credential_revision,
        **{name: getattr(target, name) for name in SETTINGS},
    )
    Region.objects.filter(pk=region.pk).update(
        revision=fresh.region_revision, approved_binding=fresh
    )
    CollectionJob.objects.create(
        binding=fresh,
        endpoint="forecasts",
        scheduled_for=approved_at,
        target_date=approved_at.astimezone(ZoneInfo(ZONE)).date() + timedelta(days=1),
        available_at=approved_at,
    )
    return fresh


def collections_after_reset(system: System, tmp_path: Path) -> ForecastSnapshot:
    """The recreated binding's own data: the historical import up to 31 October 01:00 local
    and its first live collection, the intraday estimated actuals at the default 16:00
    clock of its approval (NOW), reaching seven days back."""
    start = local(date(2026, 10, 26))
    archive(
        system,
        power_file(
            tmp_path,
            [
                {
                    "period_end": (start + timedelta(minutes=30 * (index + 1))).isoformat(),
                    "period": "PT30M",
                    "pv_power_rooftop": 0.7,
                }
                for index in range(5 * 48 + 2)
            ],
        ),
    )
    return acquisition(system.binding, "estimated_actuals", NOW, NOW - timedelta(days=7), NOW)
