from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Event
from typing import Any, cast

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import OperationalError, connection, transaction

from pyracast.accounts.models import Membership
from pyracast.accounts.operations import require_member
from pyracast.incidents.operations import save_incident
from pyracast.irradiance import local_day_bounds
from pyracast.plants import history, incident_forecasts
from pyracast.plants.history import plant_outlook
from pyracast.plants.operations import activate_calibration
from pyracast.regions.models import Region
from tests.test_domain import NOW
from tests.test_plant_operations import make_plant, publish_snapshot, ready_run, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


def test_outlook_rechecks_membership_after_waiting_for_region_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    member_checked = Event()

    def checked_member(actor: User, tenant_id: int) -> None:
        require_member(actor, tenant_id)
        member_checked.set()

    monkeypatch.setattr(history, "require_member", checked_member)

    def read() -> dict[str, object]:
        try:
            return plant_outlook(
                system.member,
                system.tenant.pk,
                system.region.pk,
                plant.pk,
                target_date=date(2026, 11, 10),
                resolution_minutes=60,
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            Region.objects.select_for_update().get(pk=system.region.pk)
            future = pool.submit(read)
            assert member_checked.wait(timeout=10)
            Membership.objects.filter(user=system.member, tenant=system.tenant).delete()
        with pytest.raises(PermissionDenied, match="Tenant membership required"):
            future.result(timeout=10)


def test_outlook_serializes_incident_writes_until_scenario_is_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    ready = ready_run(system, plant)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    activate_calibration(*scope, ready.pk)
    target = date(2026, 11, 10)
    publish_snapshot(snapshot_for(system, target, collected_at=NOW))
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
