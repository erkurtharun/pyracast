from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.outlook import SOURCES, Source
from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasConnection
from pyracast.plants.models import CalibrationRun, Plant, PlantConfiguration, ProductionObservation
from pyracast.plants.performance import production_comparison
from pyracast.regions.models import Region
from tests.support.domain import NOW, System
from tests.support.forecast_performance import publish
from tests.support.performance_history import (
    FIRST,
    LAST,
    activated_calibration,
    estimated_actuals,
    performance_history,
)
from tests.support.plant_operations import half_hours, make_plant, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)

DAY = date(2026, 11, 8)
# Factor 0.8 on 52 MW over the 21 daylight half hours of 8 November.
CALIBRATED_SATELLITE_MWH = 0.8 * 52 * 10.5
QUERY_CEILINGS: dict[Source, int] = {"forecast": 5, "satellite": 11}


def _path(system: System, plant: Plant, endpoint: str = "performance") -> str:
    return (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/{endpoint}/"
    )


def _client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _get(client: Client, path: str, **query: str) -> dict[str, Any]:
    response = client.get(path, query)
    assert response.status_code == 200, response.json()
    body: dict[str, Any] = response.json()
    return body


def _figure(shown: dict[str, Any]) -> float | None:
    daylight = (shown["result"] or {}).get("daylight") or {}
    return daylight.get("energy_mwh") if daylight.get("complete") else None


def _production(system: System, plant: Plant, day: date, total_mwh: float) -> None:
    start, _ = local_day_bounds(day, system.binding.timezone)
    configuration = plant.current_configuration
    assert configuration is not None
    ProductionObservation.objects.bulk_create(
        ProductionObservation(
            plant=plant,
            configuration=configuration,
            starts_at=start + timedelta(hours=hour),
            ends_at=start + timedelta(hours=hour + 1),
            energy_mwh=total_mwh / 24,
            source="meter",
            recorded_by=system.member,
        )
        for hour in range(24)
    )


def _other_location(system: System, plant: Plant, declared_at: datetime) -> System:
    """An earlier declaration of the plant on another location, valid until `declared_at`."""
    binding = SiteBinding.objects.get(pk=system.binding.pk)
    binding.pk = None
    binding.region_revision += 1
    binding.save()
    earlier = PlantConfiguration.objects.create(
        region_id=plant.region_id,
        plant=plant,
        binding=binding,
        revision=2,
        dc_capacity_mw=130,
        inverter_capacity_mw=100,
        created_by=system.member,
    )
    PlantConfiguration.objects.filter(pk=earlier.pk).update(created_at=NOW - timedelta(days=200))
    PlantConfiguration.objects.filter(plant=plant).exclude(pk=earlier.pk).update(
        created_at=declared_at
    )
    return replace(system, binding=binding)


def _populated(system: System, plant: Plant, data_source: Source, first: date) -> None:
    """Production on every day to `LAST` and the source's data: two saved forecasts a day, or a
    satellite day on the current location, the first five days on an earlier one, with three
    calibrations whose production evidence comes from EPİAŞ."""
    days = [first + timedelta(days=offset) for offset in range((LAST - first).days + 1)]
    for day in days:
        _production(system, plant, day, 30)
    if data_source == "forecast":
        for day in days:
            start, _ = local_day_bounds(day, system.binding.timezone)
            for hours, power in ((8, 0.4), (3, 0.45)):
                publish(
                    snapshot_for(
                        system, day, power_kw=power, collected_at=start - timedelta(hours=hours)
                    )
                )
        return
    earlier = _other_location(system, plant, local_day_bounds(days[5], "Europe/Istanbul")[0])
    configuration = PlantConfiguration.objects.get(plant=plant, revision=1)
    for offset, day in enumerate(days):
        location = earlier if offset < 5 else system
        _, end = local_day_bounds(day, location.binding.timezone)
        estimated_actuals(location, day, 0.4, end + timedelta(hours=1))
    EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    for offset, training in enumerate((days[5], days[35], days[65])):
        run = activated_calibration(
            system, configuration, 0.9, training, NOW - timedelta(days=3 - offset)
        )
        CalibrationRun.objects.filter(pk=run.pk).update(production_snapshot_ids=[offset + 1])


@pytest.mark.parametrize("data_source", SOURCES)
def test_each_compared_day_shows_the_figure_of_the_forecast_read(
    monkeypatch: pytest.MonkeyPatch, data_source: str
) -> None:
    system, plant = make_plant(monkeypatch)
    _, calibrations = performance_history(system, plant)
    client = _client(system.member)

    compared = _get(
        client,
        _path(system, plant),
        start_date=FIRST.isoformat(),
        end_date=LAST.isoformat(),
        data_source=data_source,
    )

    matched = [day for day in compared["days"] if day["status"] == "matched"]
    assert compared["data_source"] == data_source
    assert len(matched) == {"forecast": 5, "satellite": 4}[data_source]
    mode = {"forecast": {"selection_mode": "best"}, "satellite": {}}[data_source]
    for day in matched:
        shown = _get(
            client,
            _path(system, plant, "forecast"),
            date=day["date"],
            data_source=data_source,
            resolution_minutes="60",
            **mode,
        )
        assert day["predicted_energy_mwh"] == _figure(shown)
        assert day["calibration_id"] == shown["calibration_id"]
        assert day["error_mwh"] == pytest.approx(
            day["predicted_energy_mwh"] - day["measured_energy_mwh"]
        )
        assert day["provisional_production"] is False
        if data_source == "forecast":
            assert shown["selection"]["mode"] == "best"
            assert day["forecast_id"] == shown["selection"]["forecast_id"]
    if data_source == "satellite":
        assert [calibrations[day["calibration_id"]] for day in matched] == [
            "early",
            "early",
            "late",
            "late",
        ]


def test_days_that_cannot_be_compared_name_their_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    performance_history(system, plant)

    reasons = {
        data_source: {
            day["date"]: day["reason"]
            for day in production_comparison(
                plant, start_date=FIRST, end_date=LAST, data_source=data_source
            )["days"]
            if day["status"] == "missing"
        }
        for data_source in SOURCES
    }

    assert reasons == {
        "forecast": {date(2026, 11, 7): "incomplete_or_invalid_day"},
        "satellite": {
            date(2026, 11, 6): "estimated_actuals_not_available",
            date(2026, 11, 7): "incomplete_or_invalid_day",
        },
    }


def test_a_satellite_day_without_a_complete_daylight_is_reported_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    performance_history(system, plant)
    day = date(2026, 11, 6)
    estimated_actuals(system, day, 0.3, NOW - timedelta(days=2), hours=(0, 10))

    shown = production_comparison(plant, start_date=day, end_date=day, data_source="satellite")

    assert [
        (row["status"], row["reason"], row["predicted_energy_mwh"]) for row in shown["days"]
    ] == [("missing", "invalid_forecast_data", None)]


def test_accuracy_summarises_the_compared_days(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    performance_history(system, plant)

    result = production_comparison(plant, start_date=FIRST, end_date=LAST)

    errors = [day["error_mwh"] for day in result["days"] if day["status"] == "matched"]
    measured = sum(
        day["measured_energy_mwh"] for day in result["days"] if day["status"] == "matched"
    )
    assert result["matched_days"] == len(errors) == 5
    assert result["daily_mae_mwh"] == pytest.approx(sum(map(abs, errors)) / 5)
    assert result["daily_bias_mwh"] == pytest.approx(sum(errors) / 5)
    assert result["relative_error"] == pytest.approx(sum(map(abs, errors)) / measured)
    without = date(2026, 11, 6)
    empty = production_comparison(
        plant, start_date=without, end_date=without, data_source="satellite"
    )
    assert (empty["matched_days"], empty["relative_error"], empty["daily_mae_mwh"]) == (
        0,
        None,
        None,
    )


def test_a_calibrated_satellite_day_shows_the_calibrated_figure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration is not None
    _, end = local_day_bounds(DAY, system.binding.timezone)
    estimated_actuals(system, DAY, 0.4, end + timedelta(hours=1))
    _production(system, plant, DAY, 30)
    activated_calibration(
        system, plant.current_configuration, 0.8, DAY - timedelta(days=6), NOW - timedelta(days=1)
    )

    [day] = production_comparison(plant, start_date=DAY, end_date=DAY, data_source="satellite")[
        "days"
    ]

    assert day["status"] == "matched"
    assert day["predicted_energy_mwh"] == pytest.approx(CALIBRATED_SATELLITE_MWH)


def test_a_day_without_production_is_compared_against_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    publish(snapshot_for(system, DAY, power_kw=0.4, collected_at=start - timedelta(hours=8)))
    _production(system, plant, DAY, 0)

    result = production_comparison(plant, start_date=DAY, end_date=DAY)

    [day] = result["days"]
    assert (day["status"], day["measured_energy_mwh"]) == ("matched", 0)
    assert day["predicted_energy_mwh"] > 0
    assert day["error_mwh"] == day["predicted_energy_mwh"]
    assert result["relative_error"] is None
    assert result["daily_mae_mwh"] == result["daily_bias_mwh"] == day["predicted_energy_mwh"]


def test_a_saved_forecast_without_a_complete_daylight_is_reported_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    partial = snapshot_for(
        system,
        DAY,
        collected_at=start - timedelta(hours=8),
        normalized_intervals=half_hours(start, start + timedelta(hours=10), 0.4),
    )
    publish(partial)
    _production(system, plant, DAY, 30)

    [day] = production_comparison(plant, start_date=DAY, end_date=DAY)["days"]

    assert (day["status"], day["reason"], day["forecast_id"]) == (
        "missing",
        "invalid_forecast_data",
        partial.pk,
    )


@pytest.mark.parametrize("data_source", SOURCES)
def test_a_populated_range_is_read_within_a_fixed_number_of_queries(
    monkeypatch: pytest.MonkeyPatch, data_source: Source
) -> None:
    system, plant = make_plant(monkeypatch)
    first = LAST - timedelta(days=89)
    _populated(system, plant, data_source, first)
    counts = {}
    for start in (first, LAST - timedelta(days=9)):
        with CaptureQueriesContext(connection) as queries:
            result = production_comparison(
                plant, start_date=start, end_date=LAST, data_source=data_source
            )
        assert result["matched_days"] == result["requested_days"]
        counts[result["requested_days"]] = len(queries)

    assert counts[90] <= QUERY_CEILINGS[data_source]
    assert counts[10] <= QUERY_CEILINGS[data_source]


def test_the_comparison_keeps_its_access_rules_and_date_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    client, path = Client(), _path(system, plant)
    day = (NOW.date() - timedelta(days=1)).isoformat()

    assert client.get(path).status_code == 401
    outsider = User.objects.create_user("outsider")
    Membership.objects.create(user=outsider, tenant=Tenant.objects.create(name="Other"))
    client.force_login(outsider)
    assert client.get(path).status_code == 403
    client.force_login(system.member)
    assert client.get(path).json()["requested_days"] == 30
    for query in (
        {"start_date": "bad"},
        {"data_source": ""},
        {"data_source": ["unknown", "satellite"]},
        {"data_source": "estimated_actuals"},
        {"comparison": "archived_forecast"},
        {"issuance_cutoff": "06:00"},
        {"daylight_only": "true"},
        {"start_date": day},
    ):
        response = client.get(path, query)
        assert (response.status_code, response.json()["code"]) == (400, "invalid_request")
    Region.objects.filter(pk=plant.region_id).update(deleted_at=NOW)
    assert client.get(path).status_code == 404
    for first, last in (
        (FIRST, FIRST - timedelta(days=1)),
        (FIRST, FIRST + timedelta(days=90)),
        (NOW.date(), NOW.date()),
        (date(2020, 1, 1), date(2020, 1, 2)),
    ):
        with pytest.raises(ValidationError):
            production_comparison(plant, start_date=first, end_date=last)
