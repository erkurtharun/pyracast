from datetime import timedelta
from typing import Any, cast

import pytest
from django.core.exceptions import ValidationError

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import ForecastResult, ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecasting import archived_forecast, plant_forecast
from pyracast.plants.models import PlantConfiguration, PlantForecast
from tests.test_domain import NOW
from tests.test_plant_operations import make_plant, publish_snapshot, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = NOW.date() - timedelta(days=1)


def test_archive_replays_previous_binding_without_writing_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    old = plant.configurations.get()
    snapshot = publish_snapshot(snapshot_for(system, DAY))
    binding = SiteBinding.objects.get(pk=system.binding.pk)
    binding.pk = None
    binding.site_id = "new-site"
    binding.capacity_kw = 1000
    binding.save()
    current = PlantConfiguration.objects.create(
        plant=plant,
        binding=binding,
        revision=old.revision + 1,
        dc_capacity_mw=130,
        created_by=system.member,
    )
    plant.current_configuration = current
    plant.save(update_fields=["current_configuration"])
    replay = archived_forecast(plant, DAY, 60, NOW)
    result = cast(dict[str, Any], replay["result"])
    assert replay["configuration_id"] == old.pk
    assert replay["snapshot_id"] == snapshot.pk
    assert result["complete"] is True
    assert result["energy_mwh"] == pytest.approx(1248)
    assert result["provenance"]["binding_id"] == old.binding_id
    assert "archived_forecast" in result["quality_flags"]
    assert "previous_configuration" in result["quality_flags"]
    assert not PlantForecast.objects.exists()
    with pytest.raises(ValidationError, match="configuration_changed"):
        plant_forecast(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            target_date=DAY,
            resolution_minutes=60,
        )


@pytest.mark.parametrize("status", ["pending", "failed", "unpublished", "late"])
def test_archive_excludes_unpublished_and_late_sources(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot = publish_snapshot(snapshot_for(system, DAY))
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    if status in {"pending", "failed"}:
        snapshot.job.status = status
        snapshot.job.save(update_fields=["status"])
    elif status == "unpublished":
        ForecastSnapshot.objects.filter(pk=snapshot.pk).update(available_at=None)
    else:
        ForecastResult.objects.filter(snapshot=snapshot).update(published_at=start)
    replay = archived_forecast(plant, DAY, 30, NOW)
    assert replay["result"] is None
    assert replay["unavailable_reason"] == "forecast_unavailable"


def test_archive_assumes_earliest_declaration_only_without_explicit_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.configurations.get()
    PlantConfiguration.objects.filter(pk=configuration.pk).update(created_at=NOW)
    publish_snapshot(snapshot_for(system, DAY))
    replay = archived_forecast(plant, DAY, 30, NOW)
    result = cast(dict[str, Any], replay["result"])
    assert "historical_configuration_assumed" in result["quality_flags"]
    assert result["factor"] == 1
    strict = archived_forecast(plant, DAY, 30, NOW, as_of=NOW - timedelta(days=2))
    assert strict["unavailable_reason"] == "configuration_unavailable"
    assert not PlantForecast.objects.exists()


def test_archive_rejects_future_days(monkeypatch: pytest.MonkeyPatch) -> None:
    _, plant = make_plant(monkeypatch)
    with pytest.raises(ValidationError, match="date_out_of_range"):
        archived_forecast(plant, NOW.date() + timedelta(days=1), 30, NOW)


def test_archive_reuses_cached_forecast_without_changing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, DAY))
    strict = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    cached = PlantForecast.objects.get()
    before = cached.payload
    replay = archived_forecast(plant, DAY, 60, NOW)
    assert (
        cast(dict[str, Any], replay["result"])["energy_mwh"]
        == cast(dict[str, Any], strict["result"])["energy_mwh"]
    )
    cached.refresh_from_db()
    assert cached.payload == before
    assert "archived_forecast" not in cached.payload["quality_flags"]
    assert PlantForecast.objects.count() == 1


@pytest.mark.parametrize("resolution", [True, 15])
def test_archive_rejects_invalid_resolution(
    monkeypatch: pytest.MonkeyPatch, resolution: int
) -> None:
    _, plant = make_plant(monkeypatch)
    with pytest.raises(ValidationError, match="Resolution"):
        archived_forecast(plant, DAY, resolution, NOW)


@pytest.mark.parametrize("bad_cutoff", [NOW.replace(tzinfo=None), NOW + timedelta(seconds=1)])
def test_archive_rejects_invalid_cutoff(monkeypatch: pytest.MonkeyPatch, bad_cutoff: Any) -> None:
    _, plant = make_plant(monkeypatch)
    with pytest.raises(ValidationError, match="as_of"):
        archived_forecast(plant, DAY, 30, NOW, as_of=bad_cutoff)


def test_archive_reports_invalid_source(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot = publish_snapshot(snapshot_for(system, DAY))
    snapshot.normalized_intervals[0]["power_kw"] = -1
    snapshot.save(update_fields=["normalized_intervals"])
    with pytest.raises(ValidationError, match="calculation_out_of_range"):
        archived_forecast(plant, DAY, 30, NOW)
