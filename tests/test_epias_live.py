import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from pyracast.epias import AUTH_URL, EpiasClient
from pyracast.metering.client import get_epias_client
from tests.support.epias import PASSWORD, PLANT, USERNAME


def test_sanitized_live_capture_retains_all_provider_hours_and_energy() -> None:
    body = json.loads(
        (Path(__file__).parent / "fixtures" / "epias_captured_generation.json").read_text(
            encoding="utf-8"
        )
    )

    def replay(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text="TGT-recorded-fixture")
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(replay)) as transport:
        result = EpiasClient(transport, username=USERNAME, password=PASSWORD).fetch_day(
            PLANT, date(2026, 11, 9)
        )
    assert len(result.readings) == 24
    assert all(row.total is not None for row in result.readings)
    assert sum(row.total or Decimal(0) for row in result.readings) == Decimal("481")
    assert max(row.total or Decimal(0) for row in result.readings) == Decimal("73.92")
    assert result.readings[0].hour == "00:00"
    assert result.readings[-1].hour == "23:00"


@pytest.mark.skipif(
    os.environ.get("PYRACAST_EPIAS_LIVE") != "1",
    reason="Live EPİAŞ access requires PYRACAST_EPIAS_LIVE=1 and explicit plant identifiers.",
)
def test_authorized_epias_live_access() -> None:
    plant_id = os.environ.get("EPIAS_LIVE_PLANT_ID")
    eic = os.environ.get("EPIAS_LIVE_EIC")
    if not plant_id or not eic:
        pytest.fail("Set EPIAS_LIVE_PLANT_ID and EPIAS_LIVE_EIC for the authorized plant.")
    client = get_epias_client()
    matches = [
        plant for plant in client.list_plants() if str(plant.id) == plant_id and plant.eic == eic
    ]
    assert len(matches) == 1
    day = datetime.now(ZoneInfo("Europe/Istanbul")).date() - timedelta(days=1)
    result = client.fetch_day(matches[0], day)
    assert result.plant.id == matches[0].id
    assert result.day == day
    assert result.unit == "MWh"
