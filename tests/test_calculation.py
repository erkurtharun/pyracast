import json
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from pyracast.calculation import calculate
from pyracast.connections.contracts import Calculator, ProviderData
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.intervals import NormalizedInterval
from pyracast.plants.forecasting import power_intervals
from pyracast.plants.models import PlantConfiguration
from pyracast.providers import FixtureProvider, normalize_payload

TARGET = date(2026, 11, 10)
COLLECTED = datetime(2026, 11, 9, 16, tzinfo=UTC)


def binding() -> SiteBinding:
    return SiteBinding(
        id=7,
        region_id=8,
        region_revision=3,
        provider_account=ProviderAccount(provider="fixture"),
        capacity_kw=2,
        capacity_semantics_verified=True,
        latitude=0,
        longitude=0,
        timezone="UTC",
    )


def data_for(row: dict[str, object] | None = None) -> ProviderData:
    intervals = [] if row is None else [row]
    return ProviderData({}, NormalizedInterval.from_rows(intervals), collected_at=COLLECTED)


def test_bridge_fits_calculator_and_outputs_explicit_json_safe_provenance() -> None:
    site = binding()
    data = replace(FixtureProvider().fetch(site, TARGET), collected_at=COLLECTED)
    calculator: Calculator = calculate
    result = calculator(site, TARGET, data)
    assert result["region_id"] == 8
    assert result["local_date"] == "2026-11-10"
    assert result["timezone"] == "UTC"
    assert result["surface"] == "horizontal"
    assert result["complete"] is True
    assert result["energy_kwh_m2"] == pytest.approx(7.2)
    assert result["partial_energy_kwh_m2"] == pytest.approx(7.2)
    assert result["coverage_fraction"] == 1
    assert result["vintage"] == {
        "provider": "fixture",
        "source_kind": "forecast",
        "collected_at": "2026-11-09T16:00:00+00:00",
        "issued_at": None,
        "binding_revision": "7:3",
    }
    assert result["quality_flags"] == ["synthetic_fixture", "unvalidated"]
    assert json.loads(json.dumps(result, allow_nan=False)) == result


@pytest.mark.parametrize(
    ("capacity_kw", "dc_capacity_kw"), [(1.0, 1.0), (1000.0, 1000.0), (1000.0, 1250.0)]
)
def test_reference_capacity_scales_power_without_changing_irradiation(
    capacity_kw: float, dc_capacity_kw: float
) -> None:
    site = binding()
    site.capacity_kw = capacity_kw
    site.dc_capacity_kw = dc_capacity_kw
    site.azimuth_degrees = 0
    result = calculate(
        site,
        TARGET,
        data_for(
            {
                "start": "2026-11-10T12:00:00Z",
                "end": "2026-11-10T12:30:00Z",
                "power_kw": 0.6 * capacity_kw,
                "sunlight": "day",
            }
        ),
    )
    assert result["intervals"] == [
        {
            "start": "2026-11-10T12:00:00+00:00",
            "end": "2026-11-10T12:30:00+00:00",
            "ghi_wm2": 600.0,
            "quality_flags": ["unvalidated"],
        }
    ]
    assert result["partial_energy_kwh_m2"] == pytest.approx(0.3)
    assert result["configuration"] == {
        "capacity_kw": capacity_kw,
        "dc_capacity_kw": dc_capacity_kw,
        "power_basis": "ac",
        "capacity_basis": "ac",
        "capacity_semantics_verified": True,
        "tilt_degrees": 0,
        "azimuth_degrees": 0,
        "efficiency_factor_percent": 100,
    }
    snapshot = ForecastSnapshot(
        normalized_intervals=[
            {
                "start": "2026-11-10T12:00:00Z",
                "end": "2026-11-10T12:30:00Z",
                "power_kw": 0.6 * capacity_kw,
            }
        ]
    )
    plant_configuration = PlantConfiguration(binding=site, dc_capacity_mw=130)
    assert power_intervals(snapshot, plant_configuration)[0].power_mw == pytest.approx(78)


def test_live_like_missing_daytime_and_issue_timestamp_are_preserved() -> None:
    raw = {
        "forecasts": [{"period_end": "2026-11-10T00:30:00Z", "period": "PT30M", "pv_estimate": 0}]
    }
    data = replace(normalize_payload(raw, collected_at=COLLECTED), issued_at=COLLECTED)
    result = calculate(binding(), TARGET, data)
    assert result["energy_kwh_m2"] is None
    assert result["partial_energy_kwh_m2"] == 0
    assert result["complete"] is False
    assert "synthetic_fixture" not in str(result["quality_flags"])
    assert "ambiguous_zero_power" in str(result["quality_flags"])
    assert result["vintage"] == {
        "provider": "fixture",
        "source_kind": "forecast",
        "collected_at": COLLECTED.isoformat(),
        "issued_at": COLLECTED.isoformat(),
        "binding_revision": "7:3",
    }


def test_missing_collection_time_is_not_invented() -> None:
    with pytest.raises(ValueError, match="collection timestamp"):
        calculate(binding(), TARGET, ProviderData({}, ()))


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"start": "2026-11-10T00:00:00Z", "end": 5},
        {"start": "bad", "end": "2026-11-10T00:30:00Z"},
        {"start": "2026-11-10T00:00:00Z", "end": "2026-11-10T00:30:00Z", "power_kw": True},
        {"start": "2026-11-10T00:00:00Z", "end": "2026-11-10T00:30:00Z", "power_kw": "1"},
        {"start": "2026-11-10T00:00:00Z", "end": "2026-11-10T00:30:00Z", "sunlight": "sunny"},
    ],
)
def test_malformed_normalized_rows_fail_explicitly(row: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"Normalized|Invalid"):
        calculate(binding(), TARGET, data_for(row))


def test_missing_normalized_power_and_default_sunlight_are_preserved() -> None:
    result = calculate(
        binding(),
        TARGET,
        data_for({"start": "2026-11-10T00:00:00Z", "end": "2026-11-10T00:30:00Z"}),
    )
    assert result["energy_kwh_m2"] is None
    assert "missing_power" in str(result["quality_flags"])
