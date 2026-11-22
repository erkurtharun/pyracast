import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from django.utils import timezone

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.secrets import encrypt_secret
from pyracast.forecasts.historical_upload import import_historical_upload, parse_historical_upload
from pyracast.forecasts.models import HistoricalReferenceImport
from tests.support.domain import NOW, System, make_system

REFERENCE = "Authorized test historical export; declared model settings verified"


@dataclass(frozen=True)
class SourceSettings:
    product: str
    unit: str
    timestamp_convention: str
    period: str
    latitude: float
    longitude: float
    capacity_kw: float
    dc_capacity_kw: float
    tilt_degrees: float
    azimuth_degrees: float
    losses_percent: float
    mounting_type: str
    source_reference: str


def source_settings(**updates: Any) -> SourceSettings:
    return replace(
        SourceSettings(
            product="historical_rooftop_pv",
            unit="kW",
            timestamp_convention="period_end",
            period="PT30M",
            latitude=40,
            longitude=29,
            capacity_kw=1,
            dc_capacity_kw=1,
            tilt_degrees=0,
            azimuth_degrees=0,
            losses_percent=0,
            mounting_type="fixed",
            source_reference=REFERENCE,
        ),
        **updates,
    )


def system_for_import(monkeypatch: pytest.MonkeyPatch) -> System:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    ProviderAccount.objects.filter(pk=system.account.pk).update(
        provider="solcast", encrypted_api_key=encrypt_secret("test-only-solcast-key")
    )
    SiteBinding.objects.filter(pk=system.binding.pk).update(
        dc_capacity_kw=1, settings_verification_reference="Verified test model settings"
    )
    return system


def power_file(tmp_path: Path, rows: object | None = None) -> Path:
    if rows is None:
        rows = [
            {
                "period_end": "2026-11-07T22:30:00+02:00",
                "period": "PT30M",
                "pv_power_rooftop": 0,
            },
            {
                "period_end": "2026-11-07T23:00:00+02:00",
                "period": "PT30M",
                "pv_power_rooftop": 0.5,
            },
            {
                "period_end": "2026-11-08T00:00:00+02:00",
                "period": "PT30M",
                "pv_power_rooftop": 0,
            },
        ]
    path = tmp_path / "arbitrary-name.json"
    path.write_text(json.dumps({"estimated_actuals": rows}), encoding="utf-8")
    return path


def archive(
    system: System, path: Path, *, reference: str = REFERENCE, **metadata: Any
) -> HistoricalReferenceImport:
    content = path.read_bytes()
    if metadata:
        content = json.dumps(json.loads(content) | metadata).encode()
    record, _ = import_historical_upload(
        system.admin,
        system.tenant.pk,
        system.region.pk,
        system.binding.pk,
        parse_historical_upload(content, path.name),
        path.name,
        settings_confirmed=True,
        source_reference=reference,
    )
    return record
