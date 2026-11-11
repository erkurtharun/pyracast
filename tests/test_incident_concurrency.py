from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Event
from typing import Any, cast

import pytest
from django.db import OperationalError, connection, transaction

from pyracast.incidents.operations import save_incident
from pyracast.irradiance import local_day_bounds
from pyracast.plants import incident_forecasts
from pyracast.plants.history import plant_outlook
from pyracast.plants.operations import activate_calibration
from tests.test_domain import NOW
from tests.test_plant_operations import make_plant, ready_run, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


def test_outlook_serializes_incident_writes_until_scenario_is_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    ready = ready_run(system, plant)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    activate_calibration(*scope, ready.pk)
    target = date(2026, 11, 10)
    snapshot_for(system, target, collected_at=NOW)
    plant_outlook(*scope, target_date=target, resolution_minutes=60)
    reached_scenario = Event()
    finish_read = Event()
    original_scenario = incident_forecasts.incident_scenario

    def delayed_scenario(*args: Any, **kwargs: Any) -> Any:
        reached_scenario.set()
        assert finish_read.wait(timeout=10)
        return original_scenario(*args, **kwargs)

    monkeypatch.setattr(incident_forecasts, "incident_scenario", delayed_scenario)
    start, _ = local_day_bounds(date(2026, 11, 4), system.binding.timezone)
    values: dict[str, Any] = dict(
        kind="inverter",
        title="Outage",
        notes="",
        starts_at=start + timedelta(hours=9),
        ends_at=None,
        available_dc_mw=65,
        available_inverter_mw=None,
        available_export_mw=None,
    )

    def read() -> dict[str, object]:
        try:
            return plant_outlook(*scope, target_date=target, resolution_minutes=60)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(read)
        try:
            assert reached_scenario.wait(timeout=10)
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '500ms'")
                with pytest.raises(OperationalError, match="lock timeout"):
                    save_incident(*scope, **values)
        finally:
            finish_read.set()
        before = future.result(timeout=10)
    assert cast(dict[str, Any], before["result"])["factor"] == 0.5
    assert before["outage_scenario"] is None
    save_incident(*scope, **values)
    after = plant_outlook(*scope, target_date=target, resolution_minutes=60)
    assert cast(dict[str, Any], after["result"])["factor"] == 1
    assert after["outage_scenario"] is not None
