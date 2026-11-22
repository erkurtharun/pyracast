from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from pyracast.forecasts.intraday import native_actual_snapshots
from pyracast.forecasts.models import CollectionJob
from pyracast.irradiance import local_day_bounds
from tests.support.delivery_health import recent_forecast
from tests.support.domain import NOW, make_system
from tests.support.epias_api import archive_source, connected_source
from tests.support.invariants import rewritten_archive_rows
from tests.support.metering import connected
from tests.support.plant_operations import saved_actuals
from tests.support.settlement_calibration import settlement
from tests.support.trash_operations import sibling

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("identity", "expected"),
    [(None, {"id": 77, "eic": "EIC", "name": "Plant"}), ({}, {}), ({"id": 7}, {"id": 7})],
)
def test_settlement_fixture_preserves_explicit_provider_identity(
    monkeypatch: pytest.MonkeyPatch,
    identity: dict[str, object] | None,
    expected: dict[str, object],
) -> None:
    _, plant, connection = connected(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None

    snapshot = settlement(connection, configuration, NOW.date(), identity=identity)

    snapshot.refresh_from_db()
    assert snapshot.provider_plant == expected
    assert snapshot.job.provider_plant == expected


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


def test_recent_forecast_uses_the_requested_binding_job() -> None:
    system = make_system(approved=True)
    _, binding = sibling(system)
    other_job = CollectionJob.objects.create(
        binding=binding, target_date=NOW.date(), scheduled_for=NOW, available_at=NOW
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


def test_the_archive_audit_lets_only_an_unpublished_snapshot_take_its_publication() -> None:
    staged: dict[str, object] = {"raw_payload": {}, "available_at": None}
    published = staged | {"available_at": NOW.isoformat()}

    def audit(before: dict[str, object], after: dict[str, object]) -> dict[str, list[int]]:
        return rewritten_archive_rows(
            {"plants_productionobservation": {}, "forecasts_forecastsnapshot": {1: before}},
            {"plants_productionobservation": {}, "forecasts_forecastsnapshot": {1: after}},
        )

    assert audit(staged, published) == {}
    assert audit(staged, published | {"raw_payload": {"changed": True}}) == {
        "forecasts_forecastsnapshot": [1]
    }
    assert audit(published, published | {"available_at": None}) == {
        "forecasts_forecastsnapshot": [1]
    }
