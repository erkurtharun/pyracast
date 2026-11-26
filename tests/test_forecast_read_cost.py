from datetime import date

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from tests.support.domain import System
from tests.support.forecast_history import READS, TODAY, add_history, usable_calibration
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)

PAYLOAD_CEILING_BYTES = 64_000


def _urls(system: System, plant_id: int) -> dict[str, str]:
    region = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/"
    return {"plant": f"{region}plants/{plant_id}/forecast/", "region": f"{region}forecast/"}


def _costs(
    client: Client, url: str, reads: tuple[tuple[str, date], ...]
) -> dict[tuple[str, date], tuple[int, int, int]]:
    costs = {}
    for source, day in reads:
        with CaptureQueriesContext(connection) as queries:
            response = client.get(url, {"date": str(day), "data_source": source})
        costs[source, day] = (response.status_code, len(queries), len(response.content))
    return costs


@pytest.mark.parametrize("scope", ["plant", "region"])
def test_forecast_reads_cost_the_same_at_ten_times_the_history(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    system, plant = make_plant(monkeypatch)
    usable_calibration(system, plant)
    add_history(system, plant, range(1))
    client = Client()
    client.force_login(system.member)
    url = _urls(system, plant.pk)[scope]
    client.get(url, {"date": str(TODAY)})
    small = _costs(client, url, READS)
    add_history(system, plant, range(1, 10))
    large = _costs(client, url, READS)
    assert {read: cost[0] for read, cost in large.items()} == dict.fromkeys(READS, 200)
    assert {read: cost[1] for read, cost in large.items()} == {
        read: cost[1] for read, cost in small.items()
    }
    assert max(cost[2] for cost in large.values()) <= PAYLOAD_CEILING_BYTES
