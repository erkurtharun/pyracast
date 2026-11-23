import logging
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
from django.db import connection
from django.http import HttpResponseBase
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from pyracast.accounts.audit import logger as security_logger
from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.connections.models import SiteBinding
from pyracast.errors import DomainError, ErrorCode
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
from pyracast.plants.plant_days import plant_days
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
PRODUCTION_DAY = {"date", "timezone", "total_mwh", "complete", "rows"}
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
        {"date": day.isoformat(), "data_source": "auto", "resolution_minutes": "60"},
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
        "collected_at": _instant(answer["collected_at"]),
        "rows": [
            {**row, "start": _instant(row["start"]), "end": _instant(row["end"])}
            for row in answer["rows"]
        ],
    }


def _instant(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def forecast_window(system: System) -> None:
    """Estimated actuals for the 7 past days and a forecast for today and the 14 days after."""
    for offset in range(-7, 0):
        day = TODAY + timedelta(days=offset)
        _, end = local_day_bounds(day, system.binding.timezone)
        estimated_actuals(system, day, 0.3, end + timedelta(hours=1))
    for offset in range(15):
        publish_snapshot(
            snapshot_for(
                system, TODAY + timedelta(days=offset), collected_at=NOW - timedelta(hours=1)
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
    estimated_actuals(system, day, 0.3, end + timedelta(hours=1))
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
    assert {day["source_kind"] for day in body["forecast"][:7]} == {"estimated_actuals"}
    assert {day["source_kind"] for day in body["forecast"][7:]} == {"forecast"}
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
        day = TODAY + timedelta(days=offset)
        _, end = local_day_bounds(day, system.binding.timezone)
        estimated_actuals(system, day, 0.3, end + timedelta(hours=1))
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
        for offset in range(-7, 0)
    ] == [None] * 3 + [run.pk] * 4
    days = by_date(body["forecast"])
    assert days[superseded]["total_mwh"] < days[superseded + timedelta(days=1)]["total_mwh"]
    last = days[TODAY + timedelta(days=14)]
    assert last["partial"] is True
    assert last["total_mwh"] is not None
    assert [row["mwh"] is None for row in last["rows"]] == [False] * 10 + [True] * 14


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
        {"date": TODAY.isoformat(), "data_source": "auto", "resolution_minutes": "60"},
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
        "collected_at": None,
        "total_mwh": None,
        "partial": False,
        "affected": None,
        "rows": [],
    }
    assert empty_past["unavailable_reason"] == "estimated_actuals_unavailable"
    assert empty_future["unavailable_reason"] == "forecast_unavailable"
    for day in (empty_past, empty_future):
        assert day["rows"] == []
        assert day["total_mwh"] is None
        assert day == same_instants(
            reduced_forecast(forecast_read(client, system, plant, date.fromisoformat(day["date"])))
        )


@pytest.mark.parametrize(
    "damage", ["06.11.2026 00:00", MISSING, None, 7], ids=["text", "missing", "null", "number"]
)
def test_a_damaged_past_day_is_refused_while_every_other_day_is_answered(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, damage: object
) -> None:
    system, plant = make_plant(monkeypatch)
    forecast_window(system)
    for offset in range(1, 31):
        metered(system, plant, TODAY - timedelta(days=offset))
    before = days_of(system, plant)
    damaged = TODAY - timedelta(days=3)
    _, end = local_day_bounds(damaged, system.binding.timezone)
    snapshot = estimated_actuals(system, damaged, 0.3, end + timedelta(hours=2))
    rows = [dict(row) for row in snapshot.normalized_intervals]
    if damage is MISSING:
        del rows[3]["start"]
    else:
        rows[3]["start"] = damage
    stored_again(snapshot, normalized_intervals=rows)
    with caplog.at_level(logging.WARNING, logger=FAULTS):
        after = days_of(system, plant)
    refused = by_date(after["forecast"])[damaged]
    assert refused == {
        "date": damaged.isoformat(),
        "timezone": "Europe/Istanbul",
        "source_kind": "estimated_actuals",
        "freshness": "missing",
        "stale_reason": None,
        "unavailable_reason": "calculation_out_of_range",
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
    [warning] = [record for record in caplog.records if record.name == FAULTS]
    assert damaged.isoformat() in warning.getMessage()
    assert "ValueError" in warning.getMessage()


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
    issuance = ForecastIssuance.objects.get(snapshot_id=old_id)
    payloads = deepcopy(issuance.payloads)
    result = payloads["60"]
    stored = result["provenance"] if field == "collected_at" else result["intervals"][3]
    if damage is MISSING:
        del stored[field]
    else:
        stored[field] = damage
    stored_again(issuance, payloads=payloads)
    read = forecast_read(client_for(system.member), system, plant, REFERENCE_DAY)
    assert read["result"]["provenance"]["snapshot_id"] == old_id
    with caplog.at_level(logging.WARNING, logger=FAULTS):
        after = days_of(system, plant)
    refused = by_date(after["forecast"])[REFERENCE_DAY]
    assert refused == {
        "date": REFERENCE_DAY.isoformat(),
        "timezone": "Europe/Istanbul",
        "source_kind": "forecast",
        "freshness": "missing",
        "stale_reason": None,
        "unavailable_reason": "calculation_out_of_range",
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
    [warning] = [record for record in caplog.records if record.name == FAULTS]
    assert REFERENCE_DAY.isoformat() in warning.getMessage()
    assert f"error={fault}" in warning.getMessage()


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
                "total_mwh": day["total_mwh"],
                "complete": day["complete"],
                "rows": rows if any(row["mwh"] is not None for row in rows) else [],
            }
        )
    return days


def production_with_instants(day: dict[str, Any]) -> dict[str, Any]:
    return {
        **day,
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
    assert days[empty] == {
        "date": empty.isoformat(),
        "timezone": "Europe/Istanbul",
        "total_mwh": None,
        "complete": False,
        "rows": [],
    }
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
        "status"
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
            _, end = local_day_bounds(day, system.binding.timezone)
            estimated_actuals(system, day, 0.3 + copy / 100, end + timedelta(minutes=59 - copy))
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


@pytest.mark.parametrize(
    ("calibration", "per_forecast_day"),
    [(None, 9), ("usable", 14), ("provider-backed", 16)],
    ids=["without-calibration", "one-usable-calibration", "provider-backed-calibration"],
)
def test_the_queries_stay_within_the_bound_per_forecast_day(
    monkeypatch: pytest.MonkeyPatch, calibration: str | None, per_forecast_day: int
) -> None:
    system, plant = make_plant(monkeypatch)
    if calibration == "usable":
        usable_calibration(system, plant)
    forecast_window(system)
    for offset in range(1, 31):
        metered(system, plant, TODAY - timedelta(days=offset))
    client = client_for(system.member)
    if calibration == "provider-backed":
        run = provider_backed_calibration(system, plant)
        assert {
            forecast_read(client, system, plant, day)["calibration_id"]
            for day in (TODAY - timedelta(days=1), TODAY, TODAY + timedelta(days=14))
        } == {run.pk}
    client.get(plant_path(system, plant))
    forecast_days = 22
    count = queries_of(client, system, plant)
    assert count <= 40 + per_forecast_day * forecast_days, count


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


@pytest.mark.parametrize("copies", [range(0), range(1, 10)], ids=["ordinary", "tenfold-history"])
def test_the_days_read_takes_at_most_25_single_day_forecast_reads(
    monkeypatch: pytest.MonkeyPatch, copies: range
) -> None:
    system, plant = make_plant(monkeypatch)
    usable_calibration(system, plant)
    forecast_window(system)
    older_sources(system, plant, range(1))
    history_behind_every_day_but_today(system, copies)
    for offset in range(1, 31):
        metered(system, plant, TODAY - timedelta(days=offset))
    client = client_for(system.member)
    parameters = {"date": TODAY.isoformat(), "data_source": "auto", "resolution_minutes": "60"}
    days, single = best_of_five_alternately(
        lambda: client.get(plant_path(system, plant)),
        lambda: client.get(plant_path(system, plant, "forecast"), parameters),
    )
    assert days <= 25 * single, (days, single)
