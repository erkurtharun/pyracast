import logging
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.test import Client

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import CollectionJob, ForecastSnapshot
from pyracast.forecasts.timing import approval_job
from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection, EpiasSnapshot
from pyracast.plants.forecast_archive import forecast_cutoff
from pyracast.plants.models import ForecastIssuance, Plant, ProductionObservation
from tests.support.domain import System
from tests.support.forecast_history import DAY, TODAY
from tests.support.invariants import stored_again
from tests.support.plant_operations import make_plant, publish_snapshot, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
FAULTS = "pyracast.forecasts.faults"

ZONE = ZoneInfo("Europe/Istanbul")
PREVIOUS = DAY - timedelta(days=1)


def _at(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), ZONE)


def _fetch(
    system: System,
    collected_at: datetime,
    power_kw: float,
    *,
    day: date = DAY,
    job: CollectionJob | None = None,
) -> ForecastSnapshot:
    stored: dict[str, Any] = {} if job is None else {"job": job}
    return publish_snapshot(
        snapshot_for(system, day, power_kw=power_kw, collected_at=collected_at, **stored)
    )


def _owners_example(system: System) -> dict[str, ForecastSnapshot]:
    """Update times 04:00, 10:00 and 17:00 around the target day."""
    return {
        "previous 04:00": _fetch(system, _at(PREVIOUS, 4), 0.30),
        "previous 10:00": _fetch(system, _at(PREVIOUS, 10), 0.44),
        "previous 17:00": _fetch(system, _at(PREVIOUS, 17), 0.42),
        "day 04:00": _fetch(system, _at(DAY, 4), 0.40),
        "day 10:00": _fetch(system, _at(DAY, 10), 0.20),
    }


def _client(system: System) -> Client:
    client = Client()
    client.force_login(system.member)
    return client


def _plant_path(system: System, plant: Plant) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"


def _region_path(system: System) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"


def _read(client: Client, path: str, day: date = DAY, **query: str | int) -> dict[str, Any]:
    response = client.get(path, {"date": day.isoformat(), **query})
    assert response.status_code == 200, response.json()
    body: dict[str, Any] = response.json()
    return body


def _total(client: Client, path: str, snapshot: ForecastSnapshot) -> float:
    body = _read(client, path, resolution_minutes=60, forecast_id=snapshot.pk)
    return float(body["result"]["daylight"]["energy_mwh"])


def _ids(body: dict[str, Any]) -> list[int]:
    return [option["forecast_id"] for option in body["selection"]["options"]]


def _shown(body: dict[str, Any]) -> tuple[str, str | None, int | None]:
    selection = body["selection"]
    return selection["mode"], selection["rule"], selection["forecast_id"]


def _manual_production(
    system: System, plant: Plant, total_mwh: float, hours: range = range(24)
) -> None:
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    assert plant.current_configuration is not None
    for hour in hours:
        ProductionObservation.objects.create(
            plant=plant,
            configuration=plant.current_configuration,
            starts_at=start + timedelta(hours=hour),
            ends_at=start + timedelta(hours=hour + 1),
            energy_mwh=total_mwh / 24,
            source="meter",
            recorded_by=system.member,
        )


def _epias_production(system: System, plant: Plant, kind: str, total_mwh: float) -> None:
    configuration = plant.current_configuration
    assert configuration is not None
    connection, _ = EpiasConnection.objects.get_or_create(
        plant=plant,
        defaults={
            "provider_plant_id": 2,
            "settlement_plant_id": 77,
            "eic": "EIC",
            "name": "Plant",
            "connected_by": system.member,
        },
    )
    start, end = local_day_bounds(DAY, "Europe/Istanbul")
    stamp = end + timedelta(hours=2 if kind == "realtime_generation" else 3)
    identity = {"id": 77 if kind == "settlement_injection" else 2, "eic": "EIC", "name": "Plant"}
    job = EpiasCollectionJob.objects.create(
        plant=plant,
        connection=connection,
        configuration=configuration,
        connection_revision=connection.revision,
        provider_plant=identity,
        source_kind=kind,
        target_date=DAY,
        scheduled_for=stamp,
        available_at=stamp,
        status="succeeded",
    )
    EpiasSnapshot.objects.create(
        job=job,
        attempt=1,
        connection=connection,
        configuration=configuration,
        target_date=DAY,
        source_kind=kind,
        provider_plant=identity,
        collected_at=stamp,
        available_at=stamp,
        readings=[
            {
                "period_start": (start + timedelta(hours=hour)).isoformat(),
                "period_end": (start + timedelta(hours=hour + 1)).isoformat(),
                "total_mwh": str(total_mwh / 24),
            }
            for hour in range(24)
        ],
        raw_payload={},
        content_sha256="a" * 64,
    )


def test_a_past_day_offers_its_saved_forecasts_and_chooses_only_day_ahead_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    fetches = _owners_example(system)
    client, path = _client(system), _plant_path(system, plant)

    latest = _read(client, path)
    _manual_production(system, plant, _total(client, path, fetches["previous 04:00"]))
    closest = _read(client, path)

    saved = ("day 04:00", "previous 17:00", "previous 10:00", "previous 04:00")
    assert _ids(latest) == [fetches[name].pk for name in saved]
    assert _shown(latest) == ("auto", "latest", fetches["day 04:00"].pk)
    assert latest["snapshot_id"] == fetches["day 04:00"].pk
    assert _shown(closest) == ("auto", "closest_to_production", fetches["day 04:00"].pk)


def test_the_forecast_total_nearest_to_production_is_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    higher = _fetch(system, _at(PREVIOUS, 10), 0.52)
    lower = _fetch(system, _at(PREVIOUS, 17), 0.506)
    client, path = _client(system), _plant_path(system, plant)
    high, low = _total(client, path, higher), _total(client, path, lower)

    _manual_production(system, plant, low - 1)
    below = _read(client, path)
    ProductionObservation.objects.all().delete()
    _manual_production(system, plant, high + 1)
    above = _read(client, path)

    assert _shown(below) == ("auto", "closest_to_production", lower.pk)
    assert below["result"]["daylight"]["energy_mwh"] == low
    assert _shown(above) == ("auto", "closest_to_production", higher.pk)
    assert above["result"]["daylight"]["energy_mwh"] == high


def test_settled_production_that_replaces_provisional_values_changes_the_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    higher = _fetch(system, _at(PREVIOUS, 10), 0.52)
    lower = _fetch(system, _at(PREVIOUS, 17), 0.46)
    client, path = _client(system), _plant_path(system, plant)
    high, low = _total(client, path, higher), _total(client, path, lower)

    _epias_production(system, plant, "realtime_generation", high)
    provisional = _read(client, path)
    _epias_production(system, plant, "settlement_injection", low)
    settled = _read(client, path)

    assert _shown(provisional) == ("auto", "closest_to_production", higher.pk)
    assert _shown(settled) == ("auto", "closest_to_production", lower.pk)


def test_production_that_does_not_cover_daylight_counts_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    earlier = _fetch(system, _at(PREVIOUS, 10), 0.52)
    later = _fetch(system, _at(PREVIOUS, 17), 0.46)
    client, path = _client(system), _plant_path(system, plant)
    _manual_production(system, plant, _total(client, path, earlier), hours=range(12))

    assert _shown(_read(client, path)) == ("auto", "latest", later.pk)


def test_the_approval_fetch_is_always_an_option_but_shown_only_without_another_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    first = approval_job(system.binding.pk)
    assert first is not None
    job = CollectionJob.objects.get(pk=first[0])
    CollectionJob.objects.filter(pk=job.pk).update(target_date=DAY)
    job.refresh_from_db()
    approval = _fetch(system, _at(PREVIOUS, 12), 0.40, job=job)
    client, path = _client(system), _plant_path(system, plant)

    alone = _read(client, path)
    other = _fetch(system, _at(PREVIOUS, 10), 0.52)
    _manual_production(system, plant, _total(client, path, approval))
    beside = _read(client, path)

    assert _shown(alone) == ("auto", "latest", approval.pk)
    assert _ids(beside) == [approval.pk, other.pk]
    assert _shown(beside) == ("auto", "closest_to_production", other.pk)


def test_a_chosen_forecast_is_answered_from_its_saved_record_and_marked_manual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    fetches = _owners_example(system)
    client, path = _client(system), _plant_path(system, plant)

    chosen = _read(client, path, forecast_id=fetches["previous 04:00"].pk)

    assert _shown(chosen) == ("manual", None, fetches["previous 04:00"].pk)
    assert chosen["snapshot_id"] == fetches["previous 04:00"].pk
    assert chosen["issuance_id"] is not None
    assert chosen["freshness"] == "archived"


def test_a_damaged_saved_forecast_is_neither_ranked_nor_offered_and_refused_when_chosen(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    system, plant = make_plant(monkeypatch)
    damaged = _fetch(system, _at(PREVIOUS, 10), 0.30)
    healthy = _fetch(system, _at(PREVIOUS, 17), 0.44)
    issuance = ForecastIssuance.objects.get(snapshot=damaged)
    stored_again(issuance, payloads=list(issuance.payloads.values()))
    client, path = _client(system), _plant_path(system, plant)
    _manual_production(system, plant, 0)

    with caplog.at_level(logging.WARNING, logger=FAULTS):
        automatic = _read(client, path)
        chosen = _read(client, path, forecast_id=healthy.pk)
        refused = client.get(path, {"date": DAY.isoformat(), "forecast_id": str(damaged.pk)})

    assert _ids(automatic) == _ids(chosen) == [healthy.pk]
    assert _shown(automatic) == ("auto", "closest_to_production", healthy.pk)
    assert automatic["snapshot_id"] == chosen["snapshot_id"] == healthy.pk
    assert (refused.status_code, refused.json()["code"]) == (400, "calculation_out_of_range")
    warnings = [record.getMessage() for record in caplog.records if record.name == FAULTS]
    assert [warning.split(" step=")[1].split()[0] for warning in warnings] == ["candidates"] * 3
    damage = f"record=forecast_issuance:{issuance.pk} error=TypeError"
    assert all(damage in warning for warning in warnings)


def test_a_forecast_that_is_not_an_option_of_the_day_or_comes_with_satellite_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    fetches = _owners_example(system)
    other_day = _fetch(system, _at(DAY - timedelta(days=3), 4), 0.40, day=DAY - timedelta(days=3))
    client = _client(system)

    for path in (_plant_path(system, plant), _region_path(system)):
        queries: tuple[dict[str, str | int], ...] = (
            {"forecast_id": other_day.pk},
            {"forecast_id": fetches["day 10:00"].pk},
            {"forecast_id": fetches["day 04:00"].pk, "data_source": "satellite"},
        )
        for query in queries:
            response = client.get(path, {"date": DAY.isoformat(), **query})
            body = response.json()
            assert (response.status_code, body["code"]) == (400, "invalid_request")
            assert list(body["fields"]) == ["forecast_id"]


def test_today_shows_the_newest_fetch_and_a_chosen_later_fetch_without_satellite_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    early = _fetch(system, _at(TODAY, 4), 0.40, day=TODAY)
    later = _fetch(system, _at(TODAY, 10), 0.46, day=TODAY)
    client = _client(system)

    for path in (_plant_path(system, plant), _region_path(system)):
        automatic = _read(client, path, TODAY)
        chosen = _read(client, path, TODAY, forecast_id=later.pk)
        assert _ids(automatic) == [later.pk, early.pk]
        assert _shown(automatic) == ("auto", "latest", later.pk)
        assert _shown(chosen) == ("manual", None, later.pk)
        assert chosen["source_kind"] == "forecast"
        assert chosen["result"]["provenance"]["snapshot_id"] == later.pk


def test_today_offers_a_fetch_collected_before_sunrise_and_published_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    client = _client(system)
    published: dict[date, ForecastSnapshot] = {}
    for day in (DAY, TODAY):
        sunrise = forecast_cutoff(day, system.binding)
        published[day] = publish_snapshot(
            snapshot_for(system, day, collected_at=sunrise - timedelta(minutes=30)),
            published_at=sunrise + timedelta(minutes=30),
        )

    for path in (_plant_path(system, plant), _region_path(system)):
        today = _read(client, path, TODAY)
        chosen = _read(client, path, TODAY, forecast_id=published[TODAY].pk)
        assert _ids(today) == [published[TODAY].pk]
        assert _shown(chosen) == ("manual", None, published[TODAY].pk)
        assert _ids(_read(client, path)) == []


def test_a_chosen_later_fetch_is_never_answered_by_a_fetch_collected_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    earlier = snapshot_for(system, TODAY, power_kw=0.40, collected_at=_at(TODAY, 10))
    later = snapshot_for(
        system, TODAY, power_kw=0.46, collected_at=_at(TODAY, 10) + timedelta(minutes=1)
    )
    publish_snapshot(later, published_at=_at(TODAY, 10) + timedelta(minutes=5))
    publish_snapshot(earlier, published_at=_at(TODAY, 10) + timedelta(minutes=10))
    client = _client(system)

    for path in (_plant_path(system, plant), _region_path(system)):
        chosen = _read(client, path, TODAY, forecast_id=earlier.pk)
        assert _shown(chosen) == ("manual", None, earlier.pk)
        assert chosen["result"]["provenance"]["snapshot_id"] == earlier.pk
        assert later.pk not in chosen["result"]["provenance"]["source_snapshot_ids"]


def test_the_previous_sunrise_is_computed_once_per_configuration_of_the_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    _owners_example(system)
    days: list[date] = []

    def counted(day: date, binding: SiteBinding) -> datetime:
        days.append(day)
        return forecast_cutoff(day, binding)

    monkeypatch.setattr("pyracast.plants.forecast_choice.forecast_cutoff", counted)
    _read(_client(system), _plant_path(system, plant))

    assert days.count(PREVIOUS) == 1


def test_a_past_day_never_offers_or_shows_a_fetch_made_after_its_sunrise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    _fetch(system, _at(DAY, 10), 0.20)
    client = _client(system)

    for path in (_plant_path(system, plant), _region_path(system)):
        body = _read(client, path)
        assert (body["freshness"], body["unavailable_reason"]) == (
            "missing",
            "forecast_unavailable",
        )
        assert body["result"] is None
        assert _shown(body) == ("auto", "latest", None)
        assert _ids(body) == []


def test_the_region_offers_its_saved_forecasts_and_shows_the_latest_day_ahead_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    fetches = _owners_example(system)
    client, path = _client(system), _region_path(system)

    automatic = _read(client, path)
    chosen = _read(client, path, forecast_id=fetches["previous 04:00"].pk)

    saved = ("day 04:00", "previous 17:00", "previous 10:00", "previous 04:00")
    assert _ids(automatic) == [fetches[name].pk for name in saved]
    assert _shown(automatic) == ("auto", "latest", fetches["day 04:00"].pk)
    assert automatic["result"]["provenance"]["snapshot_id"] == fetches["day 04:00"].pk
    assert _shown(chosen) == ("manual", None, fetches["previous 04:00"].pk)
    assert chosen["result"]["provenance"]["snapshot_id"] == fetches["previous 04:00"].pk


def test_satellite_data_carries_no_forecast_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    client = _client(system)

    for path in (_plant_path(system, plant), _region_path(system)):
        assert _read(client, path, data_source="satellite")["selection"] is None


def test_the_removed_data_sources_and_as_of_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    client = _client(system)

    for path in (_plant_path(system, plant), _region_path(system)):
        for query in (
            {"data_source": "auto"},
            {"data_source": "archived_forecast"},
            {"data_source": "estimated_actuals"},
            {"as_of": _at(DAY, 12).isoformat()},
        ):
            response = client.get(path, {"date": DAY.isoformat(), **query})
            assert (response.status_code, response.json()["code"]) == (400, "invalid_request")


def _comparison(client: Client, system: System, plant: Plant, **query: str) -> dict[str, Any]:
    response = client.get(
        _plant_path(system, plant).removesuffix("forecast/") + "performance/",
        {"start_date": DAY.isoformat(), "end_date": DAY.isoformat(), **query},
    )
    assert response.status_code == 200, response.json()
    body: dict[str, Any] = response.json()
    return body


def test_the_comparison_matches_real_time_production_with_the_saved_forecast_nearest_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    fetches = _owners_example(system)
    client, path = _client(system), _plant_path(system, plant)
    nearest = _total(client, path, fetches["previous 10:00"])
    _epias_production(system, plant, "realtime_generation", nearest)

    shown = _read(client, path, resolution_minutes=60)
    compared = _comparison(client, system, plant)

    day = compared["days"][0]
    assert (day["status"], day["reason"], compared["matched_days"]) == ("matched", None, 1)
    assert _shown(shown) == ("auto", "closest_to_production", fetches["previous 10:00"].pk)
    assert day["forecast_id"] == fetches["previous 10:00"].pk
    assert day["predicted_energy_mwh"] == shown["result"]["daylight"]["energy_mwh"]
    assert day["measured_energy_mwh"] == pytest.approx(nearest)
    assert day["error_mwh"] == pytest.approx(0, abs=1e-9)
    assert day["provisional_production"] is True


def test_a_day_without_a_day_ahead_saved_forecast_is_not_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    _fetch(system, _at(PREVIOUS, 4), 0.30)
    _fetch(system, _at(DAY, 10), 0.20)
    _manual_production(system, plant, 5)

    compared = _comparison(_client(system), system, plant)

    day = compared["days"][0]
    assert (day["status"], day["reason"], day["forecast_id"]) == (
        "missing",
        "forecast_not_available",
        None,
    )
    assert (compared["matched_days"], compared["relative_error"]) == (0, None)
