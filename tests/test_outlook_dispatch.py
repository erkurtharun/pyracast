import json
from pathlib import Path

import pytest
from django.test import Client

from tests.support.outlook_pins import answers, outlook_history, plant_reads, region_reads
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)

PINS = json.loads((Path(__file__).parent / "support" / "outlook_pins.json").read_text())


@pytest.mark.parametrize("scope", ["plant", "region"])
def test_every_source_day_and_cutoff_answers_as_pinned(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    system, plant = make_plant(monkeypatch)
    outlook_history(system, plant)
    client = Client()
    client.force_login(system.member)
    region = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/"
    if scope == "plant":
        pinned = answers(client, f"{region}plants/{plant.pk}/forecast/", plant_reads())
    else:
        pinned = answers(client, f"{region}forecast/", region_reads())
    assert pinned == PINS[scope]
