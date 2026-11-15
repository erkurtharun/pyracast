from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Event
from typing import Any, cast

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from pyracast.forecasts.intraday import native_actual_snapshots
from pyracast.forecasts.models import CollectionJob
from pyracast.forecasts.sources import current_forecast_snapshots
from pyracast.incidents.operations import save_incident
from pyracast.irradiance import local_day_bounds
from pyracast.plants import incident_forecasts
from pyracast.plants.forecasting import forecast_payload
from pyracast.plants.history import plant_outlook
from tests.test_domain import NOW
from tests.test_plant_operations import make_plant, publish_snapshot, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 10)


@pytest.mark.parametrize("source_kind", ["forecast", "estimated_actuals"])
@pytest.mark.parametrize("snapshot_count", [1, 24])
def test_snapshot_selection_loads_only_coverage_owners(
    monkeypatch: pytest.MonkeyPatch, source_kind: str, snapshot_count: int
) -> None:
    system, _ = make_plant(monkeypatch)
    snapshots = []
    for index in range(snapshot_count):
        snapshot = snapshot_for(
            system,
            DAY,
            collected_at=NOW - timedelta(hours=snapshot_count - index),
            source_kind=source_kind,
        )
        if source_kind == "estimated_actuals":
            snapshot.job.endpoint = "estimated_actuals"
            snapshot.job.save(update_fields=("endpoint",))
        if index:
            snapshot.normalized_intervals = snapshot.normalized_intervals[24:]
        snapshots.append(publish_snapshot(snapshot))
    start, end = local_day_bounds(DAY, system.binding.timezone)
    with CaptureQueriesContext(connection) as queries:
        selected = (
            current_forecast_snapshots(
                binding_id=system.binding.pk, start=start, end=end, as_of=NOW
            )
            if source_kind == "forecast"
            else native_actual_snapshots(system.binding, start, end, NOW)
        )
    expected = [snapshots[-1].pk, snapshots[0].pk] if snapshot_count > 1 else [snapshots[0].pk]
    assert [row.pk for row in selected] == expected
    assert len(queries) == 2
    assert '"normalized_intervals"' not in queries[0]["sql"]
    assert '"raw_payload"' not in queries[0]["sql"]


@pytest.mark.parametrize("source_kind", ["forecast", "estimated_actuals"])
def test_region_rejects_coverage_changed_between_metadata_and_payload_reads(
    monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    system, _ = make_plant(monkeypatch)
    day = NOW.date()
    for index in range(2):
        snapshot = snapshot_for(
            system,
            day,
            collected_at=NOW - timedelta(hours=2 - index),
            source_kind=source_kind,
        )
        if source_kind == "estimated_actuals":
            snapshot.job.endpoint = "estimated_actuals"
            snapshot.job.save(update_fields=("endpoint",))
        if index:
            snapshot.normalized_intervals = snapshot.normalized_intervals[24:]
        publish_snapshot(snapshot)
    client = Client()
    client.force_login(system.member)
    payload_requested, changed = Event(), Event()

    def revoke_source() -> None:
        close_old_connections()
        try:
            assert payload_requested.wait(timeout=10)
            CollectionJob.objects.filter(pk=snapshot.job_id).update(status="failed")
            changed.set()
        finally:
            connection.close()

    def interleave_payload_read(
        execute: Callable[[str, Any, bool, dict[str, Any]], Any],
        sql: str,
        params: Any,
        many: bool,
        context: dict[str, Any],
    ) -> Any:
        if sql.startswith("SELECT") and '"normalized_intervals"' in sql:
            payload_requested.set()
            assert changed.wait(timeout=10)
        return execute(sql, params, many, context)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(revoke_source)
        with connection.execute_wrapper(interleave_payload_read):
            response = client.get(
                f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/",
                {"date": day.isoformat(), "data_source": source_kind},
            )
        writer.result(timeout=10)
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "invalid_request"
    assert response.json()["detail"] == ["forecast_sources_changed"]


@pytest.mark.parametrize("shadowed", [True, False])
def test_only_contributing_forecast_rows_require_valid_native_bounds(
    monkeypatch: pytest.MonkeyPatch, shadowed: bool
) -> None:
    system, plant = make_plant(monkeypatch)
    older = publish_snapshot(
        snapshot_for(system, DAY, power_kw=0.1, collected_at=NOW - timedelta(hours=1))
    )
    latest = snapshot_for(system, DAY, power_kw=0.2, collected_at=NOW)
    latest.raw_payload = {"synthetic": True}
    if not shadowed:
        latest.normalized_intervals = latest.normalized_intervals[24:]
    publish_snapshot(latest)
    older.normalized_intervals[0]["start"] = "invalid"
    older.save(update_fields=("normalized_intervals",))
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    if not shadowed:
        with pytest.raises(ValidationError, match="calculation_out_of_range"):
            plant_outlook(*scope, target_date=DAY, resolution_minutes=60)
        return
    outlook = plant_outlook(*scope, target_date=DAY, resolution_minutes=60)
    result = cast(dict[str, Any], outlook["result"])
    assert result["energy_mwh"] == pytest.approx(624)
    assert result["provenance"]["source_snapshot_ids"] == [latest.pk]
    assert (
        "synthetic_fixture" in cast(dict[str, Any], outlook["regional_irradiance"])["quality_flags"]
    )


def test_outlook_without_incidents_does_not_reload_native_snapshot_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot = publish_snapshot(snapshot_for(system, DAY, power_kw=0.2, collected_at=NOW))
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    with CaptureQueriesContext(connection) as queries:
        outlook = plant_outlook(*scope, target_date=DAY, resolution_minutes=60)
    payload_queries = [
        row["sql"]
        for row in queries
        if row["sql"].startswith("SELECT") and '"normalized_intervals"' in row["sql"]
    ]
    assert len(payload_queries) == 1
    assert outlook["outage_scenario"] is None
    assert cast(dict[str, Any], outlook["result"])["provenance"]["snapshot_id"] == snapshot.pk


def test_ended_incident_still_produces_completed_day_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    day = NOW.date() - timedelta(days=1)
    snapshot = publish_snapshot(snapshot_for(system, day, power_kw=0.2))
    start, _ = local_day_bounds(day, system.binding.timezone)
    save_incident(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        kind="panels",
        title="Disconnected panels",
        notes="",
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        available_dc_mw=65,
        available_inverter_mw=None,
        available_export_mw=None,
    )
    configuration = plant.current_configuration
    assert configuration is not None
    normal = forecast_payload(configuration, snapshot, None, day, 60)
    outlook: dict[str, Any] = {
        "result": normal,
        "configuration_id": configuration.pk,
        "plant_id": plant.pk,
        "calibration_id": None,
    }
    scenario = incident_forecasts.incident_scenario(outlook, day, 60, timezone.now())
    assert scenario is not None
    assert cast(dict[str, Any], scenario["result"])["energy_mwh"] == pytest.approx(611)
