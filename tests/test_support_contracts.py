from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from pyracast.forecasts.intraday import native_actual_snapshots
from pyracast.forecasts.models import CollectionJob
from pyracast.irradiance import local_day_bounds
from tests.support.delivery_health import recent_forecast
from tests.support.domain import NOW, make_system
from tests.support.epias_api import archive_source, connected_source
from tests.support.invariants import rewritten_archive_rows
from tests.support.plant_operations import saved_actuals
from tests.support.trash_operations import sibling
from tests.support.weather_collection import WeatherFixture

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("missing", [False, True])
def test_archived_metering_fixture_records_completion_from_its_readings(missing: bool) -> None:
    connection = connected_source()
    values: dict[int, str | None] = dict.fromkeys(range(24), "1")
    if missing:
        values[12] = None

    snapshot = archive_source(connection, "realtime_generation", values)

    snapshot.refresh_from_db()
    midnight = datetime(2020, 1, 1, tzinfo=ZoneInfo("Europe/Istanbul"))
    hours = {datetime.fromisoformat(row["period_start"]) for row in snapshot.readings}
    absent = [row for row in snapshot.readings if row["total_mwh"] is None]
    assert hours == {midnight + timedelta(hours=hour) for hour in range(24)}
    assert len(snapshot.readings) == 24
    assert len(absent) == int(missing)
    assert snapshot.complete is (not absent)


@pytest.mark.parametrize(
    ("day", "zone"),
    [(date(2026, 11, 1), "Asia/Kathmandu"), (date(2026, 10, 25), "Europe/Berlin")],
)
def test_weather_fixture_covers_local_days_with_native_utc_hours(day: date, zone: str) -> None:
    start, end = local_day_bounds(day, zone)

    weather = WeatherFixture().fetch(40, 29, start, end)

    assert weather.requested_start == start
    assert weather.requested_end == end
    assert weather.intervals[0].start <= start
    assert weather.intervals[-1].end >= end
    assert all(interval.start.timestamp() % 3600 == 0 for interval in weather.intervals)
    assert all(interval.temperature_start_c == 10 for interval in weather.intervals)


def test_recent_forecast_uses_the_requested_binding_job() -> None:
    system = make_system(approved=True)
    _, binding = sibling(system)
    other_job = CollectionJob.objects.create(
        binding=binding, target_date=NOW.date(), scheduled_slot=NOW, available_at=NOW
    )

    snapshot = recent_forecast(system)

    assert snapshot.job.binding_id == system.binding.pk
    assert snapshot.job.status == "succeeded"
    other_job.refresh_from_db()
    assert other_job.status == "pending"


def test_sibling_binding_records_its_region_location() -> None:
    system = make_system(approved=True)

    region, binding = sibling(system)

    binding.refresh_from_db()
    assert (binding.latitude, binding.longitude, binding.timezone) == (
        region.latitude,
        region.longitude,
        region.timezone,
    )
    assert system.binding.latitude == 40


def test_saved_actuals_are_available_to_native_actual_consumers() -> None:
    system = make_system(approved=True)
    day = NOW.date() - timedelta(days=1)
    start, end = local_day_bounds(day, system.binding.timezone)

    source = saved_actuals(system, day)

    assert native_actual_snapshots(system.binding, start, end, NOW) == [source]


def test_the_archive_audit_lets_only_a_production_revision_leave_the_current_set() -> None:
    observation: dict[str, object] = {"id": 5, "is_current": True}
    epias: dict[str, object] = {"id": 5, "readings": []}
    before = {
        "plants_productionobservation": {5: observation},
        "metering_epiassnapshot": {5: epias},
    }
    superseded = {**observation, "is_current": False}

    after = {**before, "plants_productionobservation": {5: superseded}}
    rewritten = {**after, "metering_epiassnapshot": {5: {**epias, "readings": [1]}}}

    assert rewritten_archive_rows(before, after) == {}
    assert rewritten_archive_rows(before, rewritten) == {"metering_epiassnapshot": [5]}
