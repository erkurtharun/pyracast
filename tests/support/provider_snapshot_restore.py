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
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastSnapshot,
    HistoricalReferenceImport,
)
from pyracast.regions.models import Region
from tests.support.domain import System
from tests.support.historical_reference_import import archive, power_file, system_for_import
from tests.support.plant_operations import half_hours

SITE = "SITE-7Q3X"
OTHER_SITE = "SITE-OTHER-2"
ZONE = "Europe/Istanbul"
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


def local(day: date, hour: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=ZoneInfo(ZONE)).astimezone(UTC)


def restore_system(monkeypatch: pytest.MonkeyPatch) -> System:
    system = system_for_import(monkeypatch)
    SiteBinding.objects.filter(pk=system.binding.pk).update(site_id=SITE)
    system.binding.refresh_from_db()
    return system


def binding_before_reset(system: System, *, site_id: str = SITE) -> SiteBinding:
    """A binding of the database the reset erased, in a workspace of its own."""
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
    collected_at: datetime,
    start: datetime,
    end: datetime,
    *,
    rows: list[dict[str, Any]] | None = None,
    weather_payload: dict[str, Any] | None = None,
    note: str | None = None,
) -> ForecastSnapshot:
    """A published acquisition as the collection, an import or a normalization stores it."""
    rows = half_hours(start, end, 0.5) if rows is None else rows
    kind = "forecast" if endpoint.endswith("forecasts") else "estimated_actuals"
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
            sha256="0" * 64,
            source_name="before-reset.json",
            source_settings={},
            imported_by=User.objects.get(username="admin"),
            imported_at=collected_at,
            interval_count=len(rows),
        )
        if endpoint == "historical_import"
        else None
    )
    job = CollectionJob.objects.create(
        binding=binding,
        reference_import=reference,
        endpoint=endpoint,
        scheduled_slot=collected_at,
        target_date=start.astimezone(ZoneInfo(ZONE)).date(),
        status="succeeded",
        available_at=collected_at,
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
    """The Solcast export of the reset tool, read from this database with its own query."""
    with connection.cursor() as cursor, cursor.cursor.copy(SOLCAST_EXPORT_SQL) as copy:
        lines = b"".join(copy)
    return gzip.compress(lines) if compress else lines


def export_rows(content: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in gzip.decompress(content).split(b"\n") if line]


def encode_rows(rows: list[dict[str, Any]]) -> bytes:
    return gzip.compress(b"".join(json.dumps(row).encode() + b"\n" for row in rows))


@dataclass(frozen=True)
class BeforeReset:
    lookback: ForecastSnapshot
    nights: tuple[ForecastSnapshot, ...]
    forecasts: tuple[ForecastSnapshot, ...]
    content: bytes


def collections_before_reset(system: System) -> BeforeReset:
    """Live collections of the erased binding (a seven-day lookback, three nights and two
    forecasts) beside an import, a scaled copy and another site, exported by the reset tool."""
    before = binding_before_reset(system)
    lookback_start = datetime(2026, 10, 29, 7, tzinfo=UTC)
    lookback_end = datetime(2026, 11, 5, 7, tzinfo=UTC)
    rows = half_hours(lookback_start, lookback_end, 0.5)
    rows[-1]["power_kw"] = None
    lookback = acquisition(
        before,
        "estimated_actuals",
        lookback_end,
        lookback_start,
        lookback_end,
        rows=rows,
        note="line" + SEPARATOR + "separator",
    )
    night_bounds = (
        (lookback_end, datetime(2026, 11, 5, 21, tzinfo=UTC)),
        (datetime(2026, 11, 5, 21, tzinfo=UTC), datetime(2026, 11, 6, 21, tzinfo=UTC)),
        (datetime(2026, 11, 6, 21, tzinfo=UTC), datetime(2026, 11, 7, 21, tzinfo=UTC)),
    )
    nights = tuple(
        acquisition(before, "estimated_actuals", end + timedelta(minutes=30), start, end)
        for start, end in night_bounds
    )
    forecasts = (
        acquisition(
            before,
            "forecasts",
            datetime(2026, 11, 5, 3, tzinfo=UTC),
            datetime(2026, 11, 4, 21, tzinfo=UTC),
            datetime(2026, 11, 7, 21, tzinfo=UTC),
            weather_payload={"source": "open-meteo"},
        ),
        acquisition(
            before,
            "forecasts",
            datetime(2026, 11, 6, 3, tzinfo=UTC),
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
    acquisition(
        binding_before_reset(system, site_id=OTHER_SITE),
        "estimated_actuals",
        datetime(2026, 11, 6, 21, 30, tzinfo=UTC),
        datetime(2026, 11, 5, 21, tzinfo=UTC),
        datetime(2026, 11, 6, 21, tzinfo=UTC),
    )
    return BeforeReset(lookback, nights, forecasts, export_content())


def collections_after_reset(system: System, tmp_path: Path) -> ForecastSnapshot:
    """The recreated binding's own data: the historical import up to 31 October 01:00 local
    and its first live seven-day lookback."""
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
    return acquisition(
        system.binding,
        "estimated_actuals",
        datetime(2026, 11, 9, 10, tzinfo=UTC),
        datetime(2026, 11, 2, 10, tzinfo=UTC),
        datetime(2026, 11, 9, 10, tzinfo=UTC),
    )
