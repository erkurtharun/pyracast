from datetime import date, timedelta

import pytest
from django.utils import timezone

from pyracast.connections.models import SiteBinding
from pyracast.irradiance import local_day_bounds
from pyracast.plants.activation import activate_calibration
from pyracast.plants.models import Plant
from pyracast.plants.operations import revise_plant
from pyracast.regions.models import Region
from tests.support.domain import System
from tests.support.plant_operations import make_plant, publish_snapshot, ready_run, snapshot_for

DAY = date(2026, 11, 11)


def reference_transition(
    monkeypatch: pytest.MonkeyPatch,
    *,
    archive_periods: int = 48,
    with_latest: bool = True,
    calibrated: bool = False,
) -> tuple[System, Plant, int]:
    system, plant = make_plant(monkeypatch)
    if calibrated:
        run = ready_run(system, plant)
        activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    old = snapshot_for(system, DAY, power_kw=0.1)
    old.normalized_intervals = old.normalized_intervals[:archive_periods]
    publish_snapshot(old)
    monkeypatch.setattr(timezone, "now", lambda: start + timedelta(hours=20, minutes=5))
    new = SiteBinding.objects.get(pk=system.binding.pk)
    new.pk = None
    new.capacity_kw = 1000
    new.dc_capacity_kw = 1000
    new.region_revision += 1
    new.save()
    Region.objects.filter(pk=system.region.pk).update(
        approved_binding=new, revision=new.region_revision
    )
    plant = revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        name="Plant",
        dc_capacity_mw=130,
        export_limit_mw=100,
    )
    system.binding = new
    if with_latest:
        latest = snapshot_for(system, DAY, power_kw=200, collected_at=timezone.now())
        latest.normalized_intervals = latest.normalized_intervals[40:]
        publish_snapshot(latest)
    return system, plant, old.pk
