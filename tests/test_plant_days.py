from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.connections.models import SiteBinding
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import Plant, PlantConfiguration, ProductionObservation
from pyracast.plants.operations import create_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.regions.models import Region
from pyracast.regions.operations import submit_region
from tests.support.domain import NOW, System
from tests.support.incident_calibration import incident
from tests.support.invariants import stored_again
from tests.support.performance_history import estimated_actuals
from tests.support.plant_operations import make_plant, publish_snapshot, snapshot_for
from tests.support.production_changes import provider
from tests.support.provisional_measurements import DAY as PROVISIONAL_DAY
from tests.support.provisional_measurements import acquisition

pytestmark = pytest.mark.django_db(transaction=True)

TODAY = date(2026, 11, 9)
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
    system: System, plant: Plant, day: date, *, minutes: int = 60, hours: int | None = None
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
                energy_mwh=minutes / 6,
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
    read = forecast_read(client_for(system.member), system, plant, day)
    assert same_instants(forecast) == same_instants(reduced_forecast(read))


def test_each_forecast_day_is_the_reduced_automatic_forecast_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    forecast_window(system)
    body = days_of(system, plant)
    client = client_for(system.member)
    days = by_date(body["forecast"])
    for offset in (-7, -1, 0, 1, 14):
        day = TODAY + timedelta(days=offset)
        assert same_instants(days[day]) == same_instants(
            reduced_forecast(forecast_read(client, system, plant, day))
        )
    assert {day["source_kind"] for day in body["forecast"][:7]} == {"estimated_actuals"}
    assert {day["source_kind"] for day in body["forecast"][7:]} == {"forecast"}
    assert all(day["rows"] and day["total_mwh"] is not None for day in body["forecast"])
    assert all(len(day["rows"]) == 24 for day in body["forecast"])
    assert all(day["affected"] is None for day in body["forecast"])


def test_a_mixed_day_names_the_sources_of_each_row(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, TODAY, collected_at=NOW - timedelta(hours=3)))
    estimated_actuals(system, TODAY, 0.35, NOW - timedelta(minutes=30), hours=(0, 10))
    publish_snapshot(snapshot_for(system, TODAY + timedelta(days=1), collected_at=NOW))
    days = by_date(days_of(system, plant)["forecast"])
    mixed, single = days[TODAY], days[TODAY + timedelta(days=1)]
    assert mixed["source_kind"] == "mixed"
    read = forecast_read(client_for(system.member), system, plant, TODAY)
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


def test_production_days_are_the_reduced_native_production_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    complete, partial, empty = (TODAY - timedelta(days=offset) for offset in (1, 2, 3))
    metered(system, plant, complete)
    metered(system, plant, partial, minutes=30, hours=8)
    configuration = plant.current_configuration
    assert configuration is not None
    acquisition((configuration, provider(system, plant)))
    body = days_of(system, plant)
    read = client_for(system.member).get(
        plant_path(system, plant, "production"),
        {
            "start_date": (TODAY - timedelta(days=30)).isoformat(),
            "end_date": (TODAY - timedelta(days=1)).isoformat(),
            "native_intervals": "true",
        },
    )
    assert read.status_code == 200
    assert [
        {
            **day,
            "rows": [
                {**row, "start": _instant(row["start"]), "end": _instant(row["end"])}
                for row in day["rows"]
            ],
        }
        for day in body["production"]
    ] == reduced_production(read.json())
    days = by_date(body["production"])
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
    assert all(len(day["rows"]) <= 50 for day in body["forecast"] + body["production"])


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


def test_the_days_read_spends_the_forecast_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    rates = cast(dict[str, str], settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {}))
    client = client_for(system.member)
    with override_settings(
        REST_FRAMEWORK={
            **settings.REST_FRAMEWORK,
            "DEFAULT_THROTTLE_RATES": {**rates, "forecast": "1/min"},
        }
    ):
        assert client.get(plant_path(system, plant)).status_code == 200
        forecast = client.get(plant_path(system, plant, "forecast"), {"date": TODAY.isoformat()})
        days = client.get(plant_path(system, plant))
    for refused in (forecast, days):
        assert refused.status_code == 429
        assert refused.json()["code"] == "throttled"


def test_the_queries_do_not_grow_with_the_intervals_of_a_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    forecast_window(system)
    client = client_for(system.member)
    client.get(plant_path(system, plant))
    metered_days = [TODAY - timedelta(days=offset) for offset in (1, 2, 3)]
    counts = []
    for minutes in (60, 30):
        ProductionObservation.objects.all().delete()
        for day in metered_days:
            metered(system, plant, day, minutes=minutes)
        with CaptureQueriesContext(connection) as queries:
            response = client.get(plant_path(system, plant))
        assert response.status_code == 200
        production = by_date(response.json()["production"])
        assert {len(production[day]["rows"]) for day in metered_days} == {24 * 60 // minutes}
        counts.append(len(queries))
    assert counts[0] == counts[1]
