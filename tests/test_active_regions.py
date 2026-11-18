import re
from pathlib import Path

import pytest

from pyracast.forecasts.models import CollectionJob
from pyracast.plants.models import Plant, active_plant
from pyracast.plants.operations import create_plant
from pyracast.regions.models import Region, active_region
from pyracast.regions.operations import submit_region
from tests.support.domain import NOW
from tests.support.plant_operations import make_plant, snapshot_for

ROOT = Path(__file__).resolve().parents[1]
LOOKUP = re.compile(r"deleted_at__|__deleted_at\b")
# The region queryset defines what is active; the trash writes, lists and purges the column.
OWNERS = {"src/pyracast/regions/models.py", "src/pyracast/trash.py"}


def test_soft_delete_lookups_are_written_only_by_their_owners() -> None:
    sources = [*(ROOT / "src" / "pyracast").rglob("*.py"), *(ROOT / "scripts").glob("*.py")]
    found = {
        path.relative_to(ROOT).as_posix()
        for path in sources
        if "migrations" not in path.parts and LOOKUP.search(path.read_text(encoding="utf-8"))
    }
    assert found - OWNERS == set()


@pytest.mark.django_db
def test_active_rows_exclude_trashed_regions_and_everything_under_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    job = snapshot_for(system, NOW.date()).job
    kept = submit_region(
        system.member,
        system.tenant.pk,
        name="Kept",
        latitude=41,
        longitude=30,
        timezone="Europe/Istanbul",
    )
    kept_plant = create_plant(
        system.member, system.tenant.pk, kept.pk, name="Kept", dc_capacity_mw=5
    )
    Region.objects.filter(pk=system.region.pk).update(deleted_at=NOW)

    assert list(Region.objects.active()) == [kept]
    assert list(Region.objects.trashed()) == [system.region]
    assert list(Plant.objects.active()) == [kept_plant]
    assert not CollectionJob.objects.filter(active_region("binding__region__")).exists()
    assert CollectionJob.objects.exclude(active_region("binding__region__")).get() == job
    assert not Plant.objects.filter(active_plant(), pk=plant.pk).exists()
