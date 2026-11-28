import logging
import re
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from threading import Event
from time import perf_counter
from typing import Any, cast

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.http import HttpResponseBase
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from psycopg import sql

from pyracast.accounts.audit import logger as security_logger
from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.connections.models import SiteBinding
from pyracast.errors import DomainError, ErrorCode
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.history import automatic_outlook
from pyracast.plants.models import (
    CalibrationRun,
    ForecastIssuance,
    Plant,
    PlantConfiguration,
    ProductionObservation,
)
from pyracast.plants.operations import create_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.plants.plant_days import _projected, plant_days
from pyracast.regions.models import Region
from pyracast.regions.operations import submit_region
from tests.support.automatic_forecast_selection import DAY as REFERENCE_DAY
from tests.support.automatic_forecast_selection import reference_transition
from tests.support.domain import NOW, System
from tests.support.forecast_history import usable_calibration
from tests.support.incident_calibration import incident
from tests.support.invariants import stored_again
from tests.support.performance_history import activated_calibration, estimated_actuals
from tests.support.plant_operations import make_plant, publish_snapshot, snapshot_for
from tests.support.production_changes import provider
from tests.support.provisional_measurements import DAY as PROVISIONAL_DAY
from tests.support.provisional_measurements import acquisition

pytestmark = pytest.mark.django_db(transaction=True)

TODAY = date(2026, 11, 9)
FAULTS = "pyracast.forecasts.faults"
MISSING = object()
ANSWER = {"plant_id", "name", "today", "forecast", "production"}
FORECAST_DAY = {
    "date",
    "timezone",
    "daylight_windows",
    "source_kind",
    "freshness",
    "stale_reason",
    "unavailable_reason",
    "collected_at",
    "total_mwh",
    "partial",
    "affected",
    "rows",
}
FORECAST_ROW = {"start", "end", "mwh", "affected_mwh", "sources"}
PRODUCTION_DAY = {"date", "timezone", "daylight_windows", "total_mwh", "complete", "rows"}
PRODUCTION_ROW = {"start", "end", "mwh", "quality", "source", "provisional"}


def plant_path(system: System, plant: Plant, endpoint: str = "days") -> str:
    return (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/{endpoint}/"
    )


def client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def viewer_of(system: System) -> User:
    viewer = User.objects.create(username="viewer")
    Membership.objects.create(user=viewer, tenant=system.tenant, role=WorkspaceRole.VIEWER)
    return viewer


def days_of(system: System, plant: Plant) -> dict[str, Any]:
    response = client_for(system.member).get(plant_path(system, plant))
    assert response.status_code == 200, response.content
    return cast(dict[str, Any], response.json())


def by_date(days: list[dict[str, Any]]) -> dict[date, dict[str, Any]]:
    return {date.fromisoformat(day["date"]): day for day in days}


def forecast_read(client: Client, system: System, plant: Plant, day: date) -> dict[str, Any]:
    response = client.get(
        plant_path(system, plant, "forecast"),
        {"date": day.isoformat(), "data_source": "forecast", "resolution_minutes": "60"},
    )
    assert response.status_code == 200, response.content
    return cast(dict[str, Any], response.json())


def screen_energy(result: dict[str, Any]) -> tuple[float | None, bool]:
    daylight = result.get("daylight")
    summary = daylight if daylight is not None else result
    partial = summary.get("complete") is not True and (daylight or {}).get("status") != (
        "polar_night"
    )
    if partial and summary.get("coverage_fraction") == 0:
        return None, partial
    return summary.get("partial_energy_mwh" if partial else "energy_mwh"), partial


def reduced_forecast(read: dict[str, Any]) -> dict[str, Any]:
    result, scenario = read["result"], read["outage_scenario"]
    windows = ((result or {}).get("daylight") or {}).get("windows")
    total, partial = screen_energy(result) if result else (None, False)
    affected_rows = {
        row["start"]: row["energy_mwh"]
        for row in (scenario or {}).get("result", {}).get("intervals", [])
    }
    mixed = read["source_kind"] == "mixed"
    rows = [
        {
            "start": row["start"],
            "end": row["end"],
            "mwh": row["energy_mwh"],
            "affected_mwh": affected_rows.get(row["start"]),
            "sources": row["source_kinds"] if mixed else None,
        }
        for row in (result or {}).get("intervals", [])
    ]
    return {
        "date": read["date"],
        "timezone": read["timezone"],
        "daylight_windows": [
            {"starts_at": window["start"], "ends_at": window["end"]} for window in windows
        ]
        if windows is not None
        else None,
        "source_kind": read["source_kind"],
        "freshness": read["freshness"],
        "stale_reason": read["stale_reason"],
        "unavailable_reason": read["unavailable_reason"],
        "collected_at": result["provenance"]["collected_at"] if result else None,
        "total_mwh": total,
        "partial": partial,
        "affected": None
        if scenario is None
        else dict(
            zip(("total_mwh", "partial"), screen_energy(scenario["result"]), strict=True),
            status=scenario["status"],
        ),
        "rows": rows
        if any(row["mwh"] is not None or row["affected_mwh"] is not None for row in rows)
        else [],
    }


def same_instants(answer: dict[str, Any]) -> dict[str, Any]:
    return {
        **answer,
        "daylight_windows": window_instants(answer["daylight_windows"]),
        "collected_at": _instant(answer["collected_at"]),
        "rows": [
            {**row, "start": _instant(row["start"]), "end": _instant(row["end"])}
            for row in answer["rows"]
        ],
    }


def _instant(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def window_instants(windows: list[dict[str, str]] | None) -> list[dict[str, datetime]] | None:
    if windows is None:
        return None
    return [{key: datetime.fromisoformat(value) for key, value in row.items()} for row in windows]


def forecast_window(system: System) -> None:
    """A forecast saved before sunrise for the 7 past days and one for today and the 14 after."""
    for offset in range(-7, 0):
        saved_before_sunrise(system, TODAY + timedelta(days=offset))
    for offset in range(15):
        publish_snapshot(
            snapshot_for(
                system, TODAY + timedelta(days=offset), collected_at=NOW - timedelta(hours=1)
            )
        )


def saved_before_sunrise(
    system: System, day: date, *, power_kw: float = 0.3, minutes: int = 0
) -> ForecastSnapshot:
    start, _ = local_day_bounds(day, system.binding.timezone)
    return publish_snapshot(
        snapshot_for(
            system,
            day,
            power_kw=power_kw,
            collected_at=start - timedelta(hours=3, minutes=minutes),
        )
    )


def metered(
    system: System,
    plant: Plant,
    day: date,
    *,
    minutes: int = 60,
    hours: int | None = None,
    mwh: float | None = None,
) -> None:
    start, end = local_day_bounds(day, system.binding.timezone)
    stop = end if hours is None else start + timedelta(hours=hours)
    step = timedelta(minutes=minutes)
    rows = []
    while start < stop:
        rows.append(
            ProductionObservation(
                plant=plant,
                configuration=plant.current_configuration,
                starts_at=start,
                ends_at=start + step,
                energy_mwh=minutes / 6 if mwh is None else mwh,
                source="meter",
                recorded_by=system.member,
            )
        )
        start += step
    ProductionObservation.objects.bulk_create(rows)


def keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in keys(item)}
    return set()


@pytest.mark.parametrize(
    ("now", "today"),
    [
        (datetime(2026, 11, 9, 20, 59, 59, tzinfo=UTC), date(2026, 11, 9)),
        (datetime(2026, 11, 9, 21, tzinfo=UTC), date(2026, 11, 10)),
    ],
    ids=["before-local-midnight", "after-local-midnight"],
)
def test_the_window_is_counted_in_plant_days_across_midnight(
    monkeypatch: pytest.MonkeyPatch, now: datetime, today: date
) -> None:
    system, plant = make_plant(monkeypatch)
    monkeypatch.setattr(timezone, "now", lambda: now)
    body = days_of(system, plant)
    assert set(body) == ANSWER
    assert body["plant_id"] == plant.pk
    assert body["name"] == "Plant"
    assert body["today"] == today.isoformat()
    assert [day["date"] for day in body["forecast"]] == [
        (today + timedelta(days=offset)).isoformat() for offset in range(-7, 15)
    ]
    assert [day["date"] for day in body["production"]] == [
        (today - timedelta(days=offset)).isoformat() for offset in range(30, 0, -1)
    ]
    assert {day["timezone"] for day in body["forecast"] + body["production"]} == {"Europe/Istanbul"}
    assert all(set(day) == FORECAST_DAY for day in body["forecast"])
    assert all(set(day) == PRODUCTION_DAY for day in body["production"])


def clock_passing_midnight_at_the_first_forecast_query(
    monkeypatch: pytest.MonkeyPatch, before: datetime, after: datetime
) -> tuple[list[datetime], AbstractContextManager[None]]:
    moment = [before]

    def advance(execute: Callable[..., Any], sql: str, *args: Any) -> Any:
        if "forecasts_" in sql:
            moment[0] = after
        return execute(sql, *args)

    monkeypatch.setattr(timezone, "now", lambda: moment[0])
    return moment, connection.execute_wrapper(advance)


def test_a_read_during_which_local_midnight_passes_answers_every_day_as_at_its_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    forecast_window(system)
    estimated_actuals(system, TODAY, 0.35, NOW - timedelta(minutes=30), hours=(0, 10))
    start, _ = local_day_bounds(TODAY, system.binding.timezone)
    incident(system, plant, start + timedelta(hours=9), None)
    for offset in range(1, 31):
        metered(system, plant, TODAY - timedelta(days=offset))
    before = datetime(2026, 11, 9, 20, 59, 59, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: before)
    at_start = days_of(system, plant)
    moment, passing = clock_passing_midnight_at_the_first_forecast_query(
        monkeypatch, before, before + timedelta(seconds=2)
    )
    with passing:
        during = days_of(system, plant)
    assert moment == [before + timedelta(seconds=2)]
    assert during["today"] == TODAY.isoformat()
    assert by_date(during["forecast"])[TODAY]["source_kind"] == "mixed"
    assert during == at_start


@pytest.mark.parametrize(("day", "hours"), [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_a_daylight_saving_day_has_one_row_per_local_hour(
    monkeypatch: pytest.MonkeyPatch, day: date, hours: int
) -> None:
    system, plant = make_plant(monkeypatch)
    Region.objects.filter(pk=system.region.pk).update(timezone="Europe/Berlin")
    SiteBinding.objects.filter(pk=system.binding.pk).update(timezone="Europe/Berlin")
    system.binding.timezone = "Europe/Berlin"
    PlantConfiguration.objects.filter(plant=plant).update(
        created_at=datetime(2025, 6, 1, tzinfo=UTC)
    )
    start, end = local_day_bounds(day, "Europe/Berlin")
    monkeypatch.setattr(timezone, "now", lambda: end + timedelta(hours=12))
    saved_before_sunrise(system, day)
    metered(system, plant, day)
    body = days_of(system, plant)
    assert body["today"] == (day + timedelta(days=1)).isoformat()
    forecast = by_date(body["forecast"])[day]
    production = by_date(body["production"])[day]
    for rows in (forecast["rows"], production["rows"]):
        assert len(rows) == hours
        assert datetime.fromisoformat(rows[0]["start"]) == start
        assert datetime.fromisoformat(rows[-1]["end"]) == end
    assert forecast["timezone"] == production["timezone"] == "Europe/Berlin"
    [light] = forecast["daylight_windows"]
    assert (
        start
        < datetime.fromisoformat(light["starts_at"])
        < datetime.fromisoformat(light["ends_at"])
        < end
    )
    assert forecast["daylight_windows"] == production["daylight_windows"]
    assert production["complete"] is True
    assert production["total_mwh"] == pytest.approx(hours * 10)
    client = client_for(system.member)
    read = forecast_read(client, system, plant, day)
    assert same_instants(forecast) == same_instants(reduced_forecast(read))
    assert production_with_instants(production) == native_production_day(client, system, plant, day)


def each_forecast_day_is_its_read(
    client: Client, system: System, plant: Plant, body: dict[str, Any]
) -> None:
    for day in body["forecast"]:
        read = forecast_read(client, system, plant, date.fromisoformat(day["date"]))
        assert same_instants(day) == same_instants(reduced_forecast(read))


def test_each_forecast_day_is_the_reduced_automatic_forecast_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    forecast_window(system)
    body = days_of(system, plant)
    each_forecast_day_is_its_read(client_for(system.member), system, plant, body)
    assert {day["source_kind"] for day in body["forecast"]} == {"forecast"}
    assert all(day["rows"] and day["total_mwh"] is not None for day in body["forecast"])
    assert all(len(day["rows"]) == 24 for day in body["forecast"])
    assert all(day["affected"] is None for day in body["forecast"])


def test_superseded_partial_and_newly_calibrated_days_are_the_reduced_forecast_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    for offset in range(-7, 0):
        saved_before_sunrise(system, TODAY + timedelta(days=offset))
    for offset in range(14):
        day = TODAY + timedelta(days=offset)
        publish_snapshot(snapshot_for(system, day, collected_at=NOW - timedelta(hours=2)))
    superseded = TODAY + timedelta(days=3)
    publish_snapshot(
        snapshot_for(system, superseded, power_kw=0.2, collected_at=NOW - timedelta(hours=1))
    )
    partial = snapshot_for(system, TODAY + timedelta(days=14), collected_at=NOW)
    partial.normalized_intervals = partial.normalized_intervals[:20]
    publish_snapshot(partial)
    run = activated_calibration(
        system, configuration, 0.8, TODAY - timedelta(days=4), NOW - timedelta(hours=1)
    )
    body = days_of(system, plant)
    client = client_for(system.member)
    each_forecast_day_is_its_read(client, system, plant, body)
    assert [
        forecast_read(client, system, plant, TODAY + timedelta(days=offset))["calibration_id"]
        for offset in range(-7, 1)
    ] == [None] * 7 + [run.pk]
    days = by_date(body["forecast"])
    assert days[superseded]["total_mwh"] < days[superseded + timedelta(days=1)]["total_mwh"]
    last = days[TODAY + timedelta(days=14)]
    assert last["partial"] is True
    assert last["total_mwh"] is not None
    assert [row["mwh"] is None for row in last["rows"]] == [False] * 10 + [True] * 14


def test_a_day_whose_forecast_covers_only_the_night_has_no_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    night = TODAY + timedelta(days=2)
    snapshot = snapshot_for(system, night, collected_at=NOW)
    snapshot.normalized_intervals = snapshot.normalized_intervals[:5]
    publish_snapshot(snapshot)
    day = by_date(days_of(system, plant)["forecast"])[night]
    assert day["partial"] is True
    assert day["total_mwh"] is None
    read = forecast_read(client_for(system.member), system, plant, night)
    assert same_instants(day) == same_instants(reduced_forecast(read))


def test_a_plant_without_a_configuration_answers_each_day_as_its_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    forecast_window(system)
    Plant.objects.filter(pk=plant.pk).update(current_configuration=None)
    body = days_of(system, plant)
    client = client_for(system.member)
    each_forecast_day_is_its_read(client, system, plant, body)
    each_production_day_is_its_native_read(client, system, plant, body)
    assert {day["unavailable_reason"] for day in body["forecast"][7:]} == {
        "configuration_unavailable"
    }
    assert all(day["rows"] == [] for day in body["forecast"][7:] + body["production"])


def test_the_reference_capacity_fallback_day_is_the_reduced_forecast_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, old_id = reference_transition(monkeypatch)
    body = days_of(system, plant)
    read = forecast_read(client_for(system.member), system, plant, REFERENCE_DAY)
    assert read["result"]["provenance"]["snapshot_id"] == old_id
    assert "reference_transition_fallback" in read["result"]["quality_flags"]
    assert same_instants(by_date(body["forecast"])[REFERENCE_DAY]) == same_instants(
        reduced_forecast(read)
    )


def test_a_mixed_day_names_the_sources_of_each_row(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, TODAY, collected_at=NOW - timedelta(hours=3)))
    estimated_actuals(system, TODAY, 0.35, NOW - timedelta(minutes=30), hours=(0, 10))
    publish_snapshot(snapshot_for(system, TODAY + timedelta(days=1), collected_at=NOW))
    days = by_date(days_of(system, plant)["forecast"])
    mixed, single = days[TODAY], days[TODAY + timedelta(days=1)]
    assert mixed["source_kind"] == "mixed"
    read = forecast_read(client_for(system.member), system, plant, TODAY)
    assert same_instants(mixed) == same_instants(reduced_forecast(read))
    assert [row["sources"] for row in mixed["rows"]] == [
        row["source_kinds"] for row in read["result"]["intervals"]
    ]
    assert {tuple(row["sources"]) for row in mixed["rows"]} >= {
        ("estimated_actuals",),
        ("forecast",),
    }
    assert single["source_kind"] == "forecast"
    assert {row["sources"] for row in single["rows"]} == {None}


@pytest.mark.parametrize(
    ("caps", "status"),
    [({}, "estimated"), ({"available_dc_mw": None}, "unknown_impact")],
    ids=["estimated", "unknown-impact"],
)
def test_an_outage_day_carries_its_affected_figures(
    monkeypatch: pytest.MonkeyPatch, caps: dict[str, Any], status: str
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(TODAY, system.binding.timezone)
    publish_snapshot(snapshot_for(system, TODAY))
    publish_snapshot(snapshot_for(system, TODAY - timedelta(days=2)))
    incident(system, plant, start + timedelta(hours=9, minutes=15), None, **caps)
    days = by_date(days_of(system, plant)["forecast"])
    outage = days[TODAY]
    read = forecast_read(client_for(system.member), system, plant, TODAY)
    assert read["outage_scenario"]["status"] == status
    assert same_instants(outage) == same_instants(reduced_forecast(read))
    assert outage["affected"]["status"] == status
    affected = [row["affected_mwh"] for row in outage["rows"]]
    assert outage["affected"]["partial"] is (status == "unknown_impact")
    assert outage["affected"]["total_mwh"] < outage["total_mwh"]
    if status == "estimated":
        assert affected[9] < outage["rows"][9]["mwh"]
    else:
        assert affected[10] is None
    assert days[TODAY - timedelta(days=2)]["affected"] is None
    assert {row["affected_mwh"] for row in days[TODAY - timedelta(days=2)]["rows"]} == {None}


def test_a_refused_or_empty_day_carries_its_reason_without_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, TODAY, collected_at=NOW - timedelta(hours=3)))
    snapshot = estimated_actuals(system, TODAY, 0.35, NOW - timedelta(minutes=30), hours=(0, 10))
    rows = [dict(row) for row in snapshot.normalized_intervals]
    rows[3]["start"] = "09.11.2026 01:30"
    stored_again(snapshot, normalized_intervals=rows)
    client = client_for(system.member)
    refusal = client.get(
        plant_path(system, plant, "forecast"),
        {"date": TODAY.isoformat(), "data_source": "forecast", "resolution_minutes": "60"},
    )
    assert refusal.status_code == 400
    assert refusal.json()["code"] == "calculation_out_of_range"
    days = by_date(days_of(system, plant)["forecast"])
    empty_past, empty_future = days[TODAY - timedelta(days=2)], days[TODAY + timedelta(days=1)]
    assert days[TODAY] == {
        "date": TODAY.isoformat(),
        "timezone": "Europe/Istanbul",
        "source_kind": "forecast",
        "freshness": "missing",
        "stale_reason": None,
        "unavailable_reason": "calculation_out_of_range",
        "daylight_windows": None,
        "collected_at": None,
        "total_mwh": None,
        "partial": False,
        "affected": None,
        "rows": [],
    }
    assert empty_past["unavailable_reason"] == "forecast_unavailable"
    assert empty_future["unavailable_reason"] == "forecast_unavailable"
    for day in (empty_past, empty_future):
        assert day["rows"] == []
        assert day["total_mwh"] is None
        assert day == same_instants(
            reduced_forecast(forecast_read(client, system, plant, date.fromisoformat(day["date"])))
        )


@pytest.mark.parametrize(
    ("damage", "fault"),
    [("06.11.2026 00:00", "ValueError"), (MISSING, "KeyError"), (None, "TypeError")],
    ids=["text", "missing", "null"],
)
def test_a_damaged_past_day_is_refused_while_every_other_day_is_answered(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, damage: object, fault: str
) -> None:
    system, plant = make_plant(monkeypatch)
    forecast_window(system)
    for offset in range(1, 31):
        metered(system, plant, TODAY - timedelta(days=offset))
    before = days_of(system, plant)
    damaged = TODAY - timedelta(days=3)
    issuance = ForecastIssuance.objects.get(target_date=damaged)
    payloads = deepcopy(issuance.payloads)
    row = payloads["60"]["intervals"][3]
    if damage is MISSING:
        del row["start"]
    else:
        row["start"] = damage
    stored_again(issuance, payloads=payloads)
    with caplog.at_level(logging.WARNING, logger=FAULTS):
        after = days_of(system, plant)
    refused = by_date(after["forecast"])[damaged]
    assert refused == {
        "date": damaged.isoformat(),
        "timezone": "Europe/Istanbul",
        "source_kind": "forecast",
        "freshness": "missing",
        "stale_reason": None,
        "unavailable_reason": "calculation_out_of_range",
        "daylight_windows": None,
        "collected_at": None,
        "total_mwh": None,
        "partial": False,
        "affected": None,
        "rows": [],
    }
    assert [day for day in after["forecast"] if day is not refused] == [
        day for day in before["forecast"] if day["date"] != damaged.isoformat()
    ]
    assert after["production"] == before["production"]
    assert damage_steps(
        caplog.records,
        f"day={damaged.isoformat()}",
        f"record=forecast_issuance:{issuance.pk}",
        fault,
    ) == ["candidates"]


def damage_steps(records: list[logging.LogRecord], *parts: str) -> list[str]:
    """The steps of the damage warnings, each of which names every part."""
    warnings = [record.getMessage() for record in records if record.name == FAULTS]
    assert all(part in warning for warning in warnings for part in parts), warnings
    return [warning.split(" step=")[1].split()[0] for warning in warnings]


def failing_on(monkeypatch: pytest.MonkeyPatch, day: date, error: Exception) -> None:
    def failing(
        plant: Plant, target_date: date, resolution_minutes: int, now: datetime
    ) -> dict[str, object]:
        if target_date == day:
            raise error
        return automatic_outlook(plant, target_date, resolution_minutes, now)

    monkeypatch.setattr("pyracast.plants.plant_days.automatic_outlook", failing)


@pytest.mark.parametrize(
    ("field", "damage", "fault"),
    [
        ("collected_at", "11.11.2026 08:00", "ValueError"),
        ("collected_at", 7, "TypeError"),
        ("start", MISSING, "KeyError"),
        ("start", "11.11.2026 03:00", "ValueError"),
        ("collected_at", "0001-01-01T00:00:00+01:00", "OverflowError"),
        ("energy_mwh", "broken", "TypeError"),
        ("energy_mwh", 10**400, "OverflowError"),
    ],
    ids=[
        "collected-at-text",
        "collected-at-number",
        "interval-without-start",
        "interval-start-text",
        "collected-at-before-the-first-utc-instant",
        "interval-energy-text",
        "interval-energy-beyond-a-float",
    ],
)
def test_an_archived_field_the_days_read_cannot_project_refuses_only_its_day(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    field: str,
    damage: object,
    fault: str,
) -> None:
    system, plant, old_id = reference_transition(monkeypatch)
    publish_snapshot(
        snapshot_for(system, REFERENCE_DAY + timedelta(days=1), collected_at=timezone.now())
    )
    before = days_of(system, plant)
    read = forecast_read(client_for(system.member), system, plant, REFERENCE_DAY)
    assert read["result"]["provenance"]["snapshot_id"] == old_id
    issuance = ForecastIssuance.objects.get(snapshot_id=old_id)
    payloads = deepcopy(issuance.payloads)
    result = payloads["60"]
    stored = result["provenance"] if field == "collected_at" else result["intervals"][3]
    if damage is MISSING:
        del stored[field]
    else:
        stored[field] = damage
    stored_again(issuance, payloads=payloads)
    archive_refused_by_both_reads(system, plant, before, issuance, caplog, fault)


def archive_refused_by_both_reads(
    system: System,
    plant: Plant,
    before: dict[str, Any],
    issuance: ForecastIssuance,
    caplog: pytest.LogCaptureFixture,
    fault: str,
) -> None:
    with caplog.at_level(logging.WARNING, logger=FAULTS):
        refusal = client_for(system.member).get(
            plant_path(system, plant, "forecast"),
            {"date": REFERENCE_DAY.isoformat(), "resolution_minutes": "60"},
        )
    assert refusal.status_code == 400, refusal.content
    assert refusal.json()["code"] == "calculation_out_of_range"
    damage = f"record=forecast_issuance:{issuance.pk} error={fault}"
    assert damage_steps(caplog.records, damage) == ["candidates", "archived_forecast"]
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=FAULTS):
        after = days_of(system, plant)
    only_the_reference_day_refused(before, after, caplog.records, fault)


@pytest.mark.parametrize(
    ("damage", "fault"),
    [
        (lambda result: result.update(intervals="broken"), "TypeError"),
        (lambda result: result["intervals"][3].update(energy_mwh=True), "TypeError"),
        (lambda result: result.update(provenance=None), "TypeError"),
        (lambda result: result["daylight"].update(energy_mwh="12"), "TypeError"),
        (lambda result: result["intervals"].__setitem__(3, "row"), "TypeError"),
    ],
    ids=[
        "intervals-not-an-array",
        "interval-energy-boolean",
        "provenance-null",
        "day-energy-text",
        "interval-not-an-object",
    ],
)
def test_an_archived_result_not_shaped_as_written_is_refused_by_both_reads(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    damage: Callable[[dict[str, Any]], None],
    fault: str,
) -> None:
    system, plant, old_id = reference_transition(monkeypatch)
    publish_snapshot(
        snapshot_for(system, REFERENCE_DAY + timedelta(days=1), collected_at=timezone.now())
    )
    before = days_of(system, plant)
    read = forecast_read(client_for(system.member), system, plant, REFERENCE_DAY)
    assert read["result"]["provenance"]["snapshot_id"] == old_id
    issuance = ForecastIssuance.objects.get(snapshot_id=old_id)
    payloads = deepcopy(issuance.payloads)
    damage(payloads["60"])
    stored_again(issuance, payloads=payloads)
    archive_refused_by_both_reads(system, plant, before, issuance, caplog, fault)


@pytest.mark.parametrize(
    "windows",
    [
        [{"start": "invalid", "end": "2026-11-09T15:00:00Z"}],
        [{"start": "2026-11-09T08:00:00", "end": "2026-11-09T15:00:00Z"}],
    ],
    ids=["invalid-instant", "missing-offset"],
)
def test_unusable_archived_daylight_bounds_keep_the_saved_figures(
    monkeypatch: pytest.MonkeyPatch, windows: list[dict[str, str]]
) -> None:
    system, plant, old_id = reference_transition(monkeypatch)
    issuance = ForecastIssuance.objects.get(snapshot_id=old_id)
    before = by_date(days_of(system, plant)["forecast"])[REFERENCE_DAY]
    assert before["rows"]
    assert before["daylight_windows"]
    payloads = deepcopy(issuance.payloads)
    payloads["60"]["daylight"]["windows"] = windows
    stored_again(issuance, payloads=payloads)
    after = by_date(days_of(system, plant)["forecast"])[REFERENCE_DAY]
    assert after == {**before, "daylight_windows": None}


def test_a_programming_error_projecting_an_archived_day_fails_the_read(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system, plant, _ = reference_transition(monkeypatch)
    projected: list[date] = []

    def defect(day: date, outlook: dict[str, Any]) -> object:
        if outlook.get("issuance_id") is not None:
            projected.append(day)
            raise KeyError("status")
        return _projected(day, outlook)

    monkeypatch.setattr("pyracast.plants.plant_days._projected", defect)
    client = Client(raise_request_exception=False)
    client.force_login(system.member)
    answer = client.get(plant_path(system, plant))
    assert answer.status_code == 500
    assert answer.json()["code"] == "server_error"
    assert [record for record in pyracast_logs.records if record.name == FAULTS] == []
    assert projected == [REFERENCE_DAY]
    defect_logged(pyracast_logs, "status")


def defect_logged(caplog: pytest.LogCaptureFixture, key: str) -> None:
    [logged] = [record for record in caplog.records if record.name == "pyracast.api"]
    assert logged.exc_info is not None
    error = logged.exc_info[1]
    assert isinstance(error, KeyError)
    assert error.args == (key,)
    assert "Traceback" in logging.Formatter().format(logged)


def stored_as_json_text(issuance: ForecastIssuance, path: list[str], number: str) -> None:
    table = sql.Identifier(ForecastIssuance._meta.db_table)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                "CREATE TEMPORARY TABLE damaged ON COMMIT DROP AS SELECT * FROM {} WHERE id = %s"
            ).format(table),
            [issuance.pk],
        )
        cursor.execute(
            "UPDATE damaged SET payloads = jsonb_set(payloads, %s, %s::jsonb)", [path, number]
        )
        cursor.execute(sql.SQL("DELETE FROM {} WHERE id = %s").format(table), [issuance.pk])
        cursor.execute(sql.SQL("INSERT INTO {} SELECT * FROM damaged").format(table))


def stored_number(issuance: ForecastIssuance, path: list[str]) -> tuple[str, str]:
    table = sql.Identifier(ForecastIssuance._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                "SELECT jsonb_typeof(payloads #> %s), payloads #>> %s FROM {} WHERE id = %s"
            ).format(table),
            [path, path, issuance.pk],
        )
        return cast(tuple[str, str], cursor.fetchone())


@pytest.mark.parametrize(
    ("path", "number", "fraction", "fault"),
    [
        (["60", "intervals", "3", "energy_mwh"], f"1{'0' * 400}.1", True, "ValueError"),
        (["60", "daylight", "energy_mwh"], f"-1{'0' * 400}.1", True, "ValueError"),
        (["60", "intervals", "3", "energy_mwh"], "1e400", False, "OverflowError"),
    ],
    ids=[
        "interval-energy-decimal-beyond-a-float",
        "day-energy-negative-decimal-beyond-a-float",
        "interval-energy-exponent-beyond-a-float",
    ],
)
def test_a_stored_number_without_a_finite_float_refuses_only_its_day(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    path: list[str],
    number: str,
    fraction: bool,
    fault: str,
) -> None:
    system, plant, old_id = reference_transition(monkeypatch)
    publish_snapshot(
        snapshot_for(system, REFERENCE_DAY + timedelta(days=1), collected_at=timezone.now())
    )
    before = days_of(system, plant)
    read = forecast_read(client_for(system.member), system, plant, REFERENCE_DAY)
    assert read["result"]["provenance"]["snapshot_id"] == old_id
    issuance = ForecastIssuance.objects.get(snapshot_id=old_id)
    stored_as_json_text(issuance, path, number)
    kind, text = stored_number(issuance, path)
    assert kind == "number"
    assert text.endswith(".1") is fraction
    with caplog.at_level(logging.WARNING, logger=FAULTS):
        after = days_of(system, plant)
    only_the_reference_day_refused(before, after, caplog.records, fault)


def only_the_reference_day_refused(
    before: dict[str, Any], after: dict[str, Any], records: list[logging.LogRecord], fault: str
) -> None:
    refused = by_date(after["forecast"])[REFERENCE_DAY]
    assert refused == {
        "date": REFERENCE_DAY.isoformat(),
        "timezone": "Europe/Istanbul",
        "source_kind": "forecast",
        "freshness": "missing",
        "stale_reason": None,
        "unavailable_reason": "calculation_out_of_range",
        "daylight_windows": None,
        "collected_at": None,
        "total_mwh": None,
        "partial": False,
        "affected": None,
        "rows": [],
    }
    assert [day for day in after["forecast"] if day is not refused] == [
        day for day in before["forecast"] if day["date"] != REFERENCE_DAY.isoformat()
    ]
    assert by_date(after["forecast"])[REFERENCE_DAY + timedelta(days=1)]["rows"]
    assert after["production"] == before["production"]
    steps = damage_steps(records, f"day={REFERENCE_DAY.isoformat()}", f"error={fault}")
    assert steps == ["candidates", "archived_forecast"]


def test_the_forecast_read_refuses_a_stored_number_without_a_finite_float(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    system, plant, old_id = reference_transition(monkeypatch)
    issuance = ForecastIssuance.objects.get(snapshot_id=old_id)
    stored_as_json_text(issuance, ["60", "intervals", "3", "energy_mwh"], f"1{'0' * 400}.1")
    with caplog.at_level(logging.WARNING, logger=FAULTS):
        refusal = client_for(system.member).get(
            plant_path(system, plant, "forecast"),
            {"date": REFERENCE_DAY.isoformat(), "resolution_minutes": "60"},
        )
    assert refusal.status_code == 400, refusal.content
    assert refusal.json()["code"] == "calculation_out_of_range"
    damage = f"record=forecast_issuance:{issuance.pk} error=ValueError"
    assert damage_steps(caplog.records, damage) == ["candidates", "archived_forecast"]


def test_a_live_snapshot_row_that_is_not_an_object_refuses_only_its_day(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    system, plant = make_plant(monkeypatch)
    forecast_window(system)
    for offset in range(1, 31):
        metered(system, plant, TODAY - timedelta(days=offset))
    before = days_of(system, plant)
    snapshot = ForecastSnapshot.objects.get(job__target_date=TODAY, source_kind="forecast")
    rows: list[Any] = [dict(row) for row in snapshot.normalized_intervals]
    rows[3] = None
    stored_again(snapshot, normalized_intervals=rows)
    with caplog.at_level(logging.WARNING, logger=FAULTS):
        after = days_of(system, plant)
    refused = by_date(after["forecast"])[TODAY]
    assert refused == {
        "date": TODAY.isoformat(),
        "timezone": "Europe/Istanbul",
        "source_kind": "forecast",
        "freshness": "missing",
        "stale_reason": None,
        "unavailable_reason": "calculation_out_of_range",
        "daylight_windows": None,
        "collected_at": None,
        "total_mwh": None,
        "partial": False,
        "affected": None,
        "rows": [],
    }
    assert [day for day in after["forecast"] if day is not refused] == [
        day for day in before["forecast"] if day["date"] != TODAY.isoformat()
    ]
    assert after["production"] == before["production"]
    [warning] = [record for record in caplog.records if record.name == FAULTS]
    assert f"record=forecast_snapshot:{snapshot.pk} error=TypeError" in warning.getMessage()
    refusal = client_for(system.member).get(
        plant_path(system, plant, "forecast"),
        {"date": TODAY.isoformat(), "data_source": "forecast", "resolution_minutes": "60"},
    )
    assert refusal.status_code == 400, refusal.content
    assert refusal.json()["code"] == "calculation_out_of_range"


STORED_TEXT = "09.11.2026 09:15"


@pytest.mark.parametrize(
    ("field", "damage", "fault"),
    [
        ("starts_at", MISSING, "KeyError"),
        ("starts_at", STORED_TEXT, "ValueError"),
        ("ends_at", "2026-11-09T18:00:00", "ValueError"),
        ("cancelled_at", False, "TypeError"),
        ("declared_configuration_id", [], "TypeError"),
        ("available_dc_mw", 10**400, "OverflowError"),
        ("available_dc_mw", -1, "ValueError"),
        ("available_inverter_mw", "65", "TypeError"),
    ],
    ids=[
        "without-start",
        "start-text",
        "naive-end",
        "cancelled-false",
        "configuration-list",
        "capacity-beyond-a-float",
        "capacity-negative",
        "capacity-text",
    ],
)
def test_the_forecast_read_refuses_a_damaged_outage_overlapping_its_day(
    monkeypatch: pytest.MonkeyPatch,
    pyracast_logs: pytest.LogCaptureFixture,
    field: str,
    damage: object,
    fault: str,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(TODAY, system.binding.timezone)
    publish_snapshot(snapshot_for(system, TODAY))
    outage = incident(system, plant, start + timedelta(hours=9, minutes=15), None)
    assert forecast_read(client_for(system.member), system, plant, TODAY)["outage_scenario"]
    revision = outage.revisions.get()
    recorded = dict(revision.snapshot)
    if damage is MISSING:
        del recorded[field]
    else:
        recorded[field] = damage
    stored_again(revision, snapshot=recorded)
    pyracast_logs.clear()
    refusal = client_for(system.member).get(
        plant_path(system, plant, "forecast"),
        {"date": TODAY.isoformat(), "data_source": "forecast", "resolution_minutes": "60"},
    )
    assert refusal.status_code == 400, refusal.content
    assert refusal.json()["code"] == "calculation_out_of_range"
    [warning] = [record for record in pyracast_logs.records if record.name == FAULTS]
    assert f"record=incident_revision:{revision.pk} error={fault}" in warning.getMessage()
    assert STORED_TEXT not in pyracast_logs.text


def test_a_meter_value_beyond_a_float_is_a_missing_hour_of_its_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    snapshot = acquisition((configuration, provider(system, plant)))
    before = days_of(system, plant)
    readings = deepcopy(snapshot.readings)
    readings[5]["total_mwh"] = 10**400
    stored_again(snapshot, readings=readings)
    after = days_of(system, plant)
    damaged = by_date(after["production"])[PROVISIONAL_DAY]
    assert production_with_instants(damaged) == native_production_day(
        client_for(system.member), system, plant, PROVISIONAL_DAY
    )
    untouched = by_date(before["production"])[PROVISIONAL_DAY]
    assert damaged["complete"] is False
    assert damaged["rows"][5]["mwh"] is None
    assert [row for index, row in enumerate(damaged["rows"]) if index != 5] == [
        row for index, row in enumerate(untouched["rows"]) if index != 5
    ]
    assert [day for day in after["production"] if day["date"] != PROVISIONAL_DAY.isoformat()] == [
        day for day in before["production"] if day["date"] != PROVISIONAL_DAY.isoformat()
    ]
    assert after["forecast"] == before["forecast"]


def test_an_outage_revision_without_a_start_refuses_only_the_days_it_can_reach(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    system, plant = make_plant(monkeypatch)
    forecast_window(system)
    for offset in range(1, 31):
        metered(system, plant, TODAY - timedelta(days=offset))
    zone = system.binding.timezone
    ended = TODAY - timedelta(days=20)
    old_start, _ = local_day_bounds(TODAY - timedelta(days=60), zone)
    start, _ = local_day_bounds(ended, zone)
    old = incident(system, plant, old_start + timedelta(hours=9), old_start + timedelta(hours=15))
    reaching = incident(system, plant, start + timedelta(hours=9), start + timedelta(hours=15))
    before = days_of(system, plant)
    for outage in (old, reaching):
        revision = outage.revisions.get()
        recorded = dict(revision.snapshot)
        del recorded["starts_at"]
        stored_again(revision, snapshot=recorded)
    with caplog.at_level(logging.WARNING, logger=FAULTS):
        after = days_of(system, plant)
    assert after["forecast"] == before["forecast"]
    for answered, earlier in zip(after["production"], before["production"], strict=True):
        if date.fromisoformat(answered["date"]) > ended:
            assert answered == earlier
        else:
            assert answered == {
                "date": answered["date"],
                "timezone": "Europe/Istanbul",
                "daylight_windows": None,
                "total_mwh": None,
                "complete": False,
                "rows": [],
            }
    warnings = [record.getMessage() for record in caplog.records if record.name == FAULTS]
    first = TODAY - timedelta(days=30)
    assert sorted(re.findall(r"day=(\S+)", "\n".join(warnings))) == [
        (first + timedelta(days=offset)).isoformat() for offset in range((ended - first).days + 1)
    ]
    revision_id = reaching.revisions.get().pk
    assert all(f"record=incident_revision:{revision_id} error=KeyError" in w for w in warnings)
    client = client_for(system.member)
    reached = client.get(
        plant_path(system, plant, "production"),
        {"start_date": ended.isoformat(), "end_date": ended.isoformat()},
    )
    assert reached.status_code == 400, reached.content
    assert reached.json()["code"] == "calculation_out_of_range"
    later = TODAY - timedelta(days=1)
    assert native_production_day(client, system, plant, later) == production_with_instants(
        by_date(after["production"])[later]
    )


def test_a_refused_production_day_carries_the_timezone_of_the_answered_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    for offset in range(1, 31):
        metered(system, plant, TODAY - timedelta(days=offset))
    ended = TODAY - timedelta(days=20)
    start, _ = local_day_bounds(ended, system.binding.timezone)
    outage = incident(system, plant, start + timedelta(hours=9), start + timedelta(hours=15))
    revision = outage.revisions.get()
    recorded = dict(revision.snapshot)
    del recorded["starts_at"]
    stored_again(revision, snapshot=recorded)
    Region.objects.filter(pk=system.region.pk).update(timezone="Europe/Berlin")
    production = by_date(days_of(system, plant)["production"])
    refused, answered = production[ended], production[TODAY - timedelta(days=1)]
    assert refused["rows"] == []
    assert refused["total_mwh"] is None
    assert answered["rows"]
    assert refused["timezone"] == answered["timezone"] == system.binding.timezone


def test_a_programming_error_inside_a_day_fails_the_read_without_a_damage_warning(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system, plant = make_plant(monkeypatch)
    forecast_window(system)

    def defect(*args: object, **kwargs: object) -> dict[str, object]:
        raise KeyError("energy_mwh")

    monkeypatch.setattr("pyracast.plants.current_forecast.forecast_payload", defect)
    client = Client(raise_request_exception=False)
    client.force_login(system.member)
    answer = client.get(plant_path(system, plant))
    assert answer.status_code == 500
    assert answer.json()["code"] == "server_error"
    assert [record for record in pyracast_logs.records if record.name == FAULTS] == []
    defect_logged(pyracast_logs, "energy_mwh")


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (RuntimeError("defect"), 500, "server_error"),
        (DomainError(ErrorCode.DATE_OUT_OF_RANGE), 400, "date_out_of_range"),
    ],
    ids=["defect", "other-refusal"],
)
def test_any_other_failure_of_a_day_fails_the_read(
    monkeypatch: pytest.MonkeyPatch, error: Exception, status: int, code: str
) -> None:
    system, plant = make_plant(monkeypatch)
    failing_on(monkeypatch, TODAY, error)
    client = Client(raise_request_exception=False)
    client.force_login(system.member)
    answer = client.get(plant_path(system, plant))
    assert answer.status_code == status
    assert answer.json()["code"] == code


def reduced_production(read: dict[str, Any]) -> list[dict[str, Any]]:
    days = []
    for day in read["days"]:
        start, end = local_day_bounds(date.fromisoformat(day["date"]), read["timezone"])
        rows = [
            {
                "start": _instant(row["starts_at"]),
                "end": _instant(row["ends_at"]),
                **{
                    name: (row["effective"] or {}).get(key)
                    for name, key in (
                        ("mwh", "energy_mwh"),
                        ("quality", "quality"),
                        ("source", "source"),
                        ("provisional", "provisional"),
                    )
                },
            }
            for row in read["intervals"]
            if start <= datetime.fromisoformat(row["starts_at"]) < end
        ]
        days.append(
            {
                "date": day["date"],
                "timezone": read["timezone"],
                "daylight_windows": window_instants(day["daylight_windows"]),
                "total_mwh": day["total_mwh"],
                "complete": day["complete"],
                "rows": rows if any(row["mwh"] is not None for row in rows) else [],
            }
        )
    return days


def production_with_instants(day: dict[str, Any]) -> dict[str, Any]:
    return {
        **day,
        "daylight_windows": window_instants(day["daylight_windows"]),
        "rows": [
            {**row, "start": _instant(row["start"]), "end": _instant(row["end"])}
            for row in day["rows"]
        ],
    }


def native_production_day(
    client: Client, system: System, plant: Plant, day: date
) -> dict[str, Any]:
    read = client.get(
        plant_path(system, plant, "production"),
        {"start_date": day.isoformat(), "end_date": day.isoformat(), "native_intervals": "true"},
    )
    assert read.status_code == 200, read.content
    [reduced] = reduced_production(read.json())
    return reduced


def each_production_day_is_its_native_read(
    client: Client, system: System, plant: Plant, body: dict[str, Any]
) -> None:
    for day in body["production"]:
        assert production_with_instants(day) == native_production_day(
            client, system, plant, date.fromisoformat(day["date"])
        )


def test_a_26_hour_day_answers_all_52_half_hourly_rows_of_its_native_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    zone, day = "Antarctica/Troll", date(2026, 10, 25)
    Region.objects.filter(pk=system.region.pk).update(timezone=zone)
    SiteBinding.objects.filter(pk=system.binding.pk).update(timezone=zone)
    system.binding.timezone = zone
    start, end = local_day_bounds(day, zone)
    assert end - start == timedelta(hours=26)
    monkeypatch.setattr(timezone, "now", lambda: end + timedelta(hours=12))
    metered(system, plant, day, minutes=30)
    production = by_date(days_of(system, plant)["production"])[day]
    assert len(production["rows"]) == 52
    assert {row["mwh"] for row in production["rows"]} == {5}
    assert production["complete"] is True
    assert production_with_instants(production) == native_production_day(
        client_for(system.member), system, plant, day
    )


def test_production_days_are_the_reduced_native_production_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    complete, partial, empty, zero, before_midnight, after_midnight = (
        TODAY - timedelta(days=offset) for offset in (1, 2, 3, 4, 6, 5)
    )
    metered(system, plant, complete)
    metered(system, plant, partial, minutes=30, hours=8)
    metered(system, plant, zero, mwh=0)
    configuration = plant.current_configuration
    assert configuration is not None
    _, midnight = local_day_bounds(before_midnight, system.binding.timezone)
    ProductionObservation.objects.create(
        plant=plant,
        configuration=configuration,
        starts_at=midnight - timedelta(minutes=30),
        ends_at=midnight + timedelta(minutes=30),
        energy_mwh=5,
        source="meter",
        recorded_by=system.member,
    )
    acquisition((configuration, provider(system, plant)))
    body = days_of(system, plant)
    each_production_day_is_its_native_read(client_for(system.member), system, plant, body)
    days = by_date(body["production"])
    assert days[zero]["complete"] is True
    assert days[zero]["total_mwh"] == 0
    assert {row["mwh"] for row in days[zero]["rows"]} == {0}
    assert len(days[zero]["rows"]) == 24
    for day in (before_midnight, after_midnight):
        assert days[day]["rows"] == []
        assert days[day]["total_mwh"] is None
    assert days[complete]["complete"] is True
    assert days[complete]["total_mwh"] == pytest.approx(240)
    assert {
        (row["quality"], row["source"], row["provisional"]) for row in days[complete]["rows"]
    } == {("valid", "meter", False)}
    assert days[partial]["complete"] is False
    assert days[partial]["total_mwh"] is None
    assert len(days[partial]["rows"]) == 16 + 16
    assert {row["mwh"] for row in days[partial]["rows"][16:]} == {None}
    assert {
        (row["quality"], row["source"], row["provisional"]) for row in days[partial]["rows"][16:]
    } == {(None, None, None)}
    assert days[empty]["rows"] == []
    assert days[empty]["total_mwh"] is None
    assert days[empty]["complete"] is False
    assert days[empty]["daylight_windows"]
    provisional = days[PROVISIONAL_DAY]
    assert provisional["complete"] is True
    assert len(provisional["rows"]) == 24
    assert {row["provisional"] for row in provisional["rows"]} == {True}


def test_members_get_one_answer_without_configuration_calibration_or_snapshot_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    forecast_window(system)
    metered(system, plant, TODAY - timedelta(days=1))
    start, _ = local_day_bounds(TODAY, system.binding.timezone)
    incident(system, plant, start + timedelta(hours=9), None)
    admin = client_for(system.member).get(plant_path(system, plant))
    viewer = client_for(viewer_of(system)).get(plant_path(system, plant))
    assert admin.status_code == viewer.status_code == 200
    assert admin.json() == viewer.json()
    body = admin.json()
    assert keys(body) == ANSWER | FORECAST_DAY | FORECAST_ROW | PRODUCTION_DAY | PRODUCTION_ROW | {
        "status",
        "starts_at",
        "ends_at",
    }
    assert all(len(day["rows"]) <= 52 for day in body["forecast"] + body["production"])


def test_only_a_member_reads_the_days_of_a_plant_of_an_active_location_in_the_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    sibling_region = submit_region(
        system.member,
        system.tenant.pk,
        name="Sibling",
        latitude=41,
        longitude=30,
        timezone="Europe/Istanbul",
    )
    sibling = create_plant(
        system.member,
        system.tenant.pk,
        sibling_region.pk,
        PlantParameters(name="Sibling", dc_capacity_mw=10),
    )
    other_member = User.objects.create(username="other-member")
    other_tenant = Tenant.objects.create(name="Other")
    Membership.objects.create(user=other_member, tenant=other_tenant, role=WorkspaceRole.ADMIN)
    foreign_region = submit_region(
        other_member,
        other_tenant.pk,
        name="Foreign",
        latitude=42,
        longitude=31,
        timezone="Europe/Istanbul",
    )
    foreign = create_plant(
        other_member,
        other_tenant.pk,
        foreign_region.pk,
        PlantParameters(name="Foreign", dc_capacity_mw=10),
    )
    member = client_for(system.member)
    assert member.get(plant_path(system, plant)).status_code == 200
    for path in (
        plant_path(system, sibling),
        plant_path(system, foreign),
        plant_path(system, plant).replace(
            f"regions/{system.region.pk}/", f"regions/{foreign_region.pk}/"
        ),
    ):
        response = member.get(path)
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"
    outsider = client_for(other_member).get(plant_path(system, plant))
    assert outsider.status_code == 403
    assert outsider.json()["code"] == "membership_required"
    Region.objects.filter(pk=system.region.pk).update(deleted_at=NOW)
    trashed = member.get(plant_path(system, plant))
    assert trashed.status_code == 404
    assert trashed.json()["code"] == "not_found"


def test_a_session_whose_membership_or_account_ended_reads_no_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    viewer = viewer_of(system)
    former_member, suspended = client_for(viewer), client_for(system.member)
    assert former_member.get(plant_path(system, plant)).status_code == 200
    assert suspended.get(plant_path(system, plant)).status_code == 200
    Membership.objects.filter(user=viewer).delete()
    User.objects.filter(pk=system.member.pk).update(is_active=False)
    ended = former_member.get(plant_path(system, plant))
    assert ended.status_code == 403
    assert ended.json()["code"] == "membership_required"
    signed_out = suspended.get(plant_path(system, plant))
    assert signed_out.status_code == 401
    assert signed_out.json()["code"] == "not_authenticated"


def one_a_minute(scope: str) -> override_settings:
    rates = cast(dict[str, str], settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {}))
    return override_settings(
        REST_FRAMEWORK={
            **settings.REST_FRAMEWORK,
            "DEFAULT_THROTTLE_RATES": {**rates, scope: "1/min"},
        }
    )


def test_the_days_read_has_a_budget_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    client = client_for(system.member)
    forecast = {"date": TODAY.isoformat()}
    with one_a_minute("days"):
        assert client.get(plant_path(system, plant)).status_code == 200
        refused = client.get(plant_path(system, plant))
        assert client.get(plant_path(system, plant, "forecast"), forecast).status_code == 200
    assert refused.status_code == 429
    assert refused.json()["code"] == "throttled"
    assert 0 < int(refused["Retry-After"]) <= 60


def test_an_exhausted_forecast_budget_leaves_the_days_read_answering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    client = client_for(system.member)
    forecast = {"date": TODAY.isoformat()}
    with one_a_minute("forecast"):
        assert client.get(plant_path(system, plant, "forecast"), forecast).status_code == 200
        assert client.get(plant_path(system, plant, "forecast"), forecast).status_code == 429
        assert client.get(plant_path(system, plant)).status_code == 200
        assert client.get(plant_path(system, plant)).status_code == 200


def held_read(monkeypatch: pytest.MonkeyPatch, outcome: Exception | None) -> tuple[Event, Event]:
    entered, release = Event(), Event()

    def held(*scope: Any) -> object:
        if not entered.is_set():
            entered.set()
            assert release.wait(30)
            if outcome is not None:
                raise outcome
        return plant_days(*scope)

    monkeypatch.setattr("pyracast.plants.api.plant_days", held)
    return entered, release


def status_of(user: User, path: str) -> int:
    try:
        client = Client(raise_request_exception=False)
        client.force_login(user)
        return client.get(path).status_code
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("outcome", "first_status"),
    [(None, 200), (RuntimeError("defect"), 500)],
    ids=["ended", "failed"],
)
def test_one_days_read_at_a_time_and_the_next_after_it_ended_or_failed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outcome: Exception | None,
    first_status: int,
) -> None:
    monkeypatch.setattr(security_logger, "handlers", [caplog.handler])
    monkeypatch.setattr(security_logger, "propagate", False)
    caplog.set_level(logging.INFO, logger="pyracast.security")
    system, plant = make_plant(monkeypatch)
    viewer = client_for(viewer_of(system))
    path = plant_path(system, plant)
    entered, release = held_read(monkeypatch, outcome)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(status_of, system.member, path)
        try:
            assert entered.wait(30)
            caplog.clear()
            meanwhile = viewer.get(path)
            events = [record for record in caplog.records if record.name == "pyracast.security"]
        finally:
            release.set()
        assert first.result(timeout=60) == first_status
    assert meanwhile.status_code == 429
    assert meanwhile.json()["code"] == "throttled"
    assert meanwhile["Retry-After"] == "5"
    assert events == []
    assert viewer.get(path).status_code == 200


def status_in(client: Client, path: str) -> int:
    try:
        return client.get(path).status_code
    finally:
        connection.close()


def test_a_second_read_of_the_same_session_waits_for_it_and_is_refused_as_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    path = plant_path(system, plant)
    first_tab = client_for(system.member)
    second_tab = Client()
    second_tab.cookies = first_tab.cookies
    entered, release = held_read(monkeypatch, None)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(status_in, first_tab, path)
        try:
            assert entered.wait(30)
            meanwhile = second_tab.get(path)
        finally:
            release.set()
        assert first.result(timeout=60) == 200
    assert meanwhile.status_code == 429
    assert meanwhile.json()["code"] == "session_busy"
    assert meanwhile["Retry-After"] == "1"
    assert second_tab.get(path).status_code == 200


def test_the_days_scope_admits_120_reads_an_hour() -> None:
    rates = cast(dict[str, str], settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])
    assert rates["days"] == "120/hour"
    assert rates["forecast"] == "60/min"


def older_sources(system: System, plant: Plant, copies: range) -> None:
    """Older acquisitions behind every forecast day of the window and newer calibration
    activations whose outage history changed; none of them is selected by a read."""
    configuration = plant.current_configuration
    assert configuration is not None
    for copy in copies:
        for offset in range(-7, 0):
            day = TODAY + timedelta(days=offset)
            saved_before_sunrise(system, day, power_kw=0.3 + copy / 100, minutes=1 + copy)
        for offset in range(15):
            publish_snapshot(
                snapshot_for(
                    system,
                    TODAY + timedelta(days=offset),
                    power_kw=0.4 + copy / 100,
                    collected_at=NOW - timedelta(hours=1, minutes=copy),
                )
            )
        activated_calibration(
            system,
            configuration,
            0.7,
            date(2026, 10, 25),
            NOW - timedelta(days=2, minutes=copy),
            incident_revision_ids=[999_999],
        )


def queries_of(client: Client, system: System, plant: Plant) -> int:
    with CaptureQueriesContext(connection) as queries:
        response = client.get(plant_path(system, plant))
    assert response.status_code == 200
    return len(queries)


def test_the_queries_do_not_grow_with_production_days_resolution_or_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    usable_calibration(system, plant)
    forecast_window(system)
    older_sources(system, plant, range(1))
    client = client_for(system.member)
    client.get(plant_path(system, plant))
    counts: dict[str, int] = {}
    for days, minutes in ((1, 60), (30, 60), (30, 30)):
        ProductionObservation.objects.all().delete()
        for offset in range(1, days + 1):
            metered(system, plant, TODAY - timedelta(days=offset), minutes=minutes)
        counts[f"{days} days at {minutes} min"] = queries_of(client, system, plant)
    older_sources(system, plant, range(1, 10))
    counts["ten times the history"] = queries_of(client, system, plant)
    assert len(set(counts.values())) == 1, counts


def provider_backed_calibration(system: System, plant: Plant) -> CalibrationRun:
    configuration = plant.current_configuration
    assert configuration is not None
    run = activated_calibration(
        system, configuration, 0.9, date(2026, 10, 20), NOW - timedelta(days=12)
    )
    evidence = acquisition((configuration, provider(system, plant)))
    CalibrationRun.objects.filter(pk=run.pk).update(production_snapshot_ids=[evidence.pk])
    return run


def queries_of_each_day_read_alone(client: Client, system: System, plant: Plant) -> int:
    count = 0
    for offset in range(-7, 15):
        with CaptureQueriesContext(connection) as queries:
            forecast_read(client, system, plant, TODAY + timedelta(days=offset))
        count += len(queries)
    for offset in range(1, 31):
        with CaptureQueriesContext(connection) as queries:
            native_production_day(client, system, plant, TODAY - timedelta(days=offset))
        count += len(queries)
    return count


@pytest.mark.parametrize(
    "calibration",
    [None, "usable", "provider-backed", "provider-backed-with-an-open-outage"],
    ids=[
        "without-calibration",
        "one-usable-calibration",
        "provider-backed-calibration",
        "provider-backed-calibration-and-an-outage-without-end-from-the-first-day",
    ],
)
def test_the_days_read_issues_no_more_queries_than_its_days_read_one_by_one(
    monkeypatch: pytest.MonkeyPatch, calibration: str | None
) -> None:
    system, plant = make_plant(monkeypatch)
    if calibration == "usable":
        usable_calibration(system, plant)
    forecast_window(system)
    for offset in range(1, 31):
        metered(system, plant, TODAY - timedelta(days=offset))
    client = client_for(system.member)
    if calibration is not None and calibration.startswith("provider-backed"):
        run = provider_backed_calibration(system, plant)
        if calibration.endswith("outage"):
            first, _ = local_day_bounds(TODAY - timedelta(days=7), system.binding.timezone)
            incident(system, plant, first, None)
        reads = [
            forecast_read(client, system, plant, day)
            for day in (TODAY - timedelta(days=7), TODAY, TODAY + timedelta(days=14))
        ]
        assert [read["calibration_id"] for read in reads] == [None, run.pk, run.pk]
        outage = calibration.endswith("outage")
        assert [read["outage_scenario"] is not None for read in reads] == [False, outage, outage]
    client.get(plant_path(system, plant))
    days = queries_of(client, system, plant)
    one_by_one = queries_of_each_day_read_alone(client, system, plant)
    assert days <= one_by_one, (days, one_by_one)


def best_of_five_alternately(*reads: Callable[[], HttpResponseBase]) -> list[float]:
    durations: list[list[float]] = [[] for _ in reads]
    for _ in range(5):
        for read, measured in zip(reads, durations, strict=True):
            start = perf_counter()
            response = read()
            measured.append(perf_counter() - start)
            assert response.status_code == 200
    return [min(measured) for measured in durations]


def history_behind_every_day_but_today(system: System, copies: range) -> None:
    for copy in copies:
        for offset in range(-7, 0):
            day = TODAY + timedelta(days=offset)
            _, end = local_day_bounds(day, system.binding.timezone)
            later = end + timedelta(hours=2, minutes=copy)
            estimated_actuals(system, day, 0.3 + copy / 100, later, hours=(0, 10))
        for offset in range(1, 15):
            publish_snapshot(
                snapshot_for(
                    system,
                    TODAY + timedelta(days=offset),
                    power_kw=0.4 + copy / 100,
                    collected_at=NOW - timedelta(hours=2, minutes=copy),
                )
            )


@pytest.mark.parametrize(
    ("copies", "per_stressed_day"),
    [(range(0), 2), (range(1, 19), 20)],
    ids=["ordinary", "tenfold-history"],
)
def test_the_days_read_takes_at_most_25_single_day_forecast_reads(
    monkeypatch: pytest.MonkeyPatch, copies: range, per_stressed_day: int
) -> None:
    system, plant = make_plant(monkeypatch)
    usable_calibration(system, plant)
    forecast_window(system)
    older_sources(system, plant, range(1))
    history_behind_every_day_but_today(system, copies)
    acquisitions = Counter(
        ForecastSnapshot.objects.filter(
            job__target_date__range=(TODAY - timedelta(days=7), TODAY + timedelta(days=14))
        ).values_list("job__target_date", flat=True)
    )
    assert acquisitions.pop(TODAY) == 2
    assert acquisitions == dict.fromkeys(acquisitions, per_stressed_day)
    assert len(acquisitions) == 21
    for offset in range(1, 31):
        metered(system, plant, TODAY - timedelta(days=offset))
    client = client_for(system.member)
    parameters = {"date": TODAY.isoformat(), "data_source": "forecast", "resolution_minutes": "60"}
    days, single = best_of_five_alternately(
        lambda: client.get(plant_path(system, plant)),
        lambda: client.get(plant_path(system, plant, "forecast"), parameters),
    )
    assert days <= 25 * single, (days, single)
