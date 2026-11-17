import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from django.utils import timezone

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.secrets import encrypt_secret
from pyracast.forecasts.historical_import import (
    HistoricalSourceSettings,
    import_historical_reference,
)
from pyracast.forecasts.models import HistoricalReferenceImport
from tests.support.domain import NOW, System, make_system


def source_settings(**updates: Any) -> HistoricalSourceSettings:
    return (
        HistoricalSourceSettings(
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
            source_reference="Authorized test historical export; declared model settings verified",
        )
        if not updates
        else replace(source_settings(), **updates)
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


def archive(system: System, path: Path, **updates: Any) -> HistoricalReferenceImport:
    return import_historical_reference(
        system.admin,
        system.tenant.pk,
        system.region.pk,
        system.binding.pk,
        path,
        source_settings(**updates),
        trusted_root=path.parent,
    )
