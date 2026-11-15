from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from django.utils import timezone

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.actuals import (
    apply_archived_actual_source,
    archived_actual_irradiance,
    replay_archived_actual_source,
    select_archived_actual_source,
)
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from tests.test_archived_actual_composition_api import DAY, acquired_actuals
from tests.test_domain import NOW, System, make_system
from tests.test_historical_reference_import import archive, power_file, system_for_import

pytestmark = pytest.mark.django_db(transaction=True)


def archived_system(monkeypatch: pytest.MonkeyPatch) -> System:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    return make_system(approved=True)


def paired_pin(system: System) -> dict[str, Any]:
    acquired_actuals(system, 7, 12, 0.1)
    acquired_actuals(system, 12, 18, 0.2)
    source = select_archived_actual_source(system.binding, DAY, NOW)
    assert source is not None
    assert isinstance(source.pin, dict)
    return deepcopy(source.pin)


def test_pinned_actual_composition_replays_original_energy_after_new_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = archived_system(monkeypatch)
    pin = paired_pin(system)
    correction = acquired_actuals(system, 7, 18, 0.8)
    ForecastSnapshot.objects.filter(pk=correction.pk).update(
        collected_at=NOW - timedelta(minutes=1), available_at=NOW - timedelta(minutes=1)
    )
    current = select_archived_actual_source(system.binding, DAY, NOW)
    frozen = replay_archived_actual_source(system.binding, DAY, NOW, pin)
    assert current is not None
    assert frozen is not None
    current_result = archived_actual_irradiance(system.binding, DAY, current)
    frozen_result = archived_actual_irradiance(system.binding, DAY, frozen)
    assert cast(dict[str, Any], current_result["daylight"])["energy_kwh_m2"] == pytest.approx(8.4)
    assert cast(dict[str, Any], frozen_result["daylight"])["energy_kwh_m2"] == pytest.approx(1.65)
    assert frozen.pin == pin
    assert (
        correction.pk
        not in cast(dict[str, Any], frozen_result["provenance"])["source_snapshot_ids"]
    )


@pytest.mark.parametrize("correction", ["missing", "null", "zero"])
def test_newer_actual_coverage_never_fills_corrected_missing_values_from_old_source(
    monkeypatch: pytest.MonkeyPatch, correction: str
) -> None:
    system = archived_system(monkeypatch)
    paired_pin(system)
    latest = acquired_actuals(system, 8, 9, 0)
    latest.collected_at = latest.available_at = NOW - timedelta(minutes=1)
    if correction == "missing":
        latest.normalized_intervals = []
    elif correction == "null":
        for row in latest.normalized_intervals:
            row["power_kw"] = None
    latest.save(update_fields=("collected_at", "available_at", "normalized_intervals"))
    source = select_archived_actual_source(system.binding, DAY, NOW)
    assert source is not None
    result = archived_actual_irradiance(system.binding, DAY, source)
    daylight = cast(dict[str, Any], result["daylight"])
    assert daylight["partial_energy_kwh_m2"] == pytest.approx(1.55)
    assert daylight["energy_kwh_m2"] is None
    assert daylight["complete"] is False
    if correction == "zero":
        assert daylight["input_coverage_fraction"] == 1
        assert "ambiguous_zero_power" in cast(list[str], result["quality_flags"])
    else:
        assert daylight["input_coverage_fraction"] < 1
    pin = source.pin
    assert isinstance(pin, dict)
    assert latest.pk in pin["snapshot_ids"]
    replay = replay_archived_actual_source(system.binding, DAY, NOW, pin)
    assert replay is not None
    assert archived_actual_irradiance(system.binding, DAY, replay)["daylight"] == result["daylight"]


def test_complete_native_day_retains_legacy_pin_and_missing_history_stays_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = archived_system(monkeypatch)
    assert select_archived_actual_source(system.binding, DAY, NOW) is None
    assert replay_archived_actual_source(system.binding, DAY, NOW, None) is None
    assert replay_archived_actual_source(system.binding, DAY, NOW, 123456) is None
    snapshot = acquired_actuals(system, 0, 24, 0.1)
    source = select_archived_actual_source(system.binding, DAY, NOW)
    assert source is not None
    assert source.pin == snapshot.pk
    assert source.composition is None
    replay = replay_archived_actual_source(system.binding, DAY, NOW, source.pin)
    assert replay is not None
    assert replay.snapshot.pk == snapshot.pk
    result = archived_actual_irradiance(system.binding, DAY, replay)
    original = deepcopy(result)
    apply_archived_actual_source(result, replay)
    assert result == original
    assert result["energy_kwh_m2"] == pytest.approx(2.4)


def test_complete_native_fragments_conserve_full_day_energy_without_legacy_vintage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = archived_system(monkeypatch)
    acquired_actuals(system, 0, 12, 0.1)
    acquired_actuals(system, 12, 24, 0.2)
    source = select_archived_actual_source(system.binding, DAY, NOW)
    assert source is not None
    assert source.composition is not None
    result = archived_actual_irradiance(system.binding, DAY, source)
    assert result["complete"] is True
    assert result["energy_kwh_m2"] == pytest.approx(3.6)
    assert cast(dict[str, object], result["provenance"])["input_day_complete"] is True


def test_complete_authorized_import_keeps_its_whole_vintage_when_native_actuals_are_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system = system_for_import(monkeypatch)
    native = acquired_actuals(system, 8, 9, 0.6)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    path = power_file(
        tmp_path,
        [
            {
                "period_end": (start + timedelta(minutes=30 * (index + 1))).isoformat(),
                "period": "PT30M",
                "pv_power_rooftop": 0.3,
            }
            for index in range(48)
        ],
    )
    record = archive(system, path)
    source = select_archived_actual_source(system.binding, DAY, NOW)
    assert source is not None
    assert source.composition is None
    assert source.snapshot.job.reference_import_id == record.pk
    assert source.snapshot.pk != native.pk
    assert source.pin == source.snapshot.pk
    result = archived_actual_irradiance(system.binding, DAY, source)
    assert result["energy_kwh_m2"] == pytest.approx(7.2)
    assert cast(dict[str, object], result["daylight"])["energy_kwh_m2"] == pytest.approx(3.15)


@pytest.mark.parametrize(
    "damage",
    [
        "power",
        "duplicate_row",
        "overlap",
        "owner",
        "day",
        "unfinished_native",
        "duplicate_id",
        "bool_id",
        "format",
    ],
)
def test_replay_rejects_fabricated_or_ambiguous_native_contributions(
    monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    system = archived_system(monkeypatch)
    pin = paired_pin(system)
    first = pin["intervals"][0]
    if damage == "power":
        first["power_kw"] = 0.9
    elif damage == "duplicate_row":
        pin["intervals"].append(deepcopy(first))
    elif damage == "overlap":
        pin["intervals"][1]["start"] = first["start"]
    elif damage == "owner":
        first["source_snapshot_id"] = 123456
    elif damage == "day":
        first["start"] = (datetime.fromisoformat(first["start"]) - timedelta(days=1)).isoformat()
    elif damage == "unfinished_native":
        first["original_end"] = (NOW + timedelta(seconds=1)).isoformat()
    elif damage == "duplicate_id":
        pin["snapshot_ids"].append(pin["snapshot_ids"][0])
    elif damage == "bool_id":
        pin["snapshot_ids"][0] = True
    else:
        pin["format"] = "unrecognized"
    with pytest.raises(ValueError, match=r"(?i)archived actual|native estimated-actual"):
        replay_archived_actual_source(system.binding, DAY, NOW, pin)


@pytest.mark.parametrize(
    "ineligible", ["collected_at", "available_at", "issued_at", "binding", "day"]
)
def test_pinned_replay_requires_original_binding_and_knowledge_cutoff(
    monkeypatch: pytest.MonkeyPatch, ineligible: str
) -> None:
    system = archived_system(monkeypatch)
    pin = paired_pin(system)
    binding = system.binding
    day = DAY
    if ineligible == "binding":
        binding = SiteBinding.objects.create(
            region=system.region,
            provider_account=system.account,
            region_revision=system.region.revision + 1,
            site_id="other-revision",
            capacity_kw=1,
            latitude=40,
            longitude=29,
            timezone=system.binding.timezone,
        )
    elif ineligible == "day":
        day += timedelta(days=1)
    else:
        ForecastSnapshot.objects.filter(pk=pin["snapshot_ids"][0]).update(
            **{ineligible: NOW + timedelta(seconds=1)}
        )
    with pytest.raises(ValueError, match=r"(?i)archived actual|native estimated-actual"):
        replay_archived_actual_source(binding, day, NOW, pin)
