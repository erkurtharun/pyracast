import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from django.db import connection

from pyracast.epias import (
    BASE_URL,
    TIMEZONE,
    EpiasClient,
    EpiasDay,
    EpiasPlant,
    EpiasReading,
)
from pyracast.metering.models import EpiasConnection
from pyracast.metering.operations import connect_epias
from tests.support.domain import NOW
from tests.support.metering import PROVIDER_PLANT, FixtureClient
from tests.support.plant_operations import make_plant

SETTLEMENT = EpiasPlant(7001, PROVIDER_PLANT.eic, PROVIDER_PLANT.name, "Solar")


def month_data(plant: EpiasPlant, start: date, end: date) -> EpiasDay:
    stamp = datetime.combine(start, datetime.min.time(), TIMEZONE)
    readings = tuple(
        EpiasReading(stamp + timedelta(hours=i), f"{i % 24:02}:00", Decimal("1.5"), None)
        for i in range(((end - start).days + 1) * 24)
    )
    return EpiasDay(
        plant,
        start,
        readings,
        NOW,
        json.dumps(
            {
                "items": [
                    {"date": r.timestamp.isoformat(), "hour": r.timestamp.hour, "total": 1.5}
                    for r in readings
                ]
            }
        ),
        endpoint=f"{BASE_URL}/injection-quantity",
        source_kind="settlement_injection",
        end_day=end,
    )


class SettlementClient(FixtureClient):
    def resolve_settlement_plant(self, eic: str) -> EpiasPlant | None:
        assert not connection.in_atomic_block
        assert eic == SETTLEMENT.eic
        return SETTLEMENT

    def list_settlement_plants(self) -> tuple[EpiasPlant, ...]:
        assert not connection.in_atomic_block
        return (SETTLEMENT,)

    def fetch_settlement(
        self, plant: EpiasPlant, start: date, end: date, *, deadline: float | None = None
    ) -> EpiasDay:
        assert not connection.in_atomic_block
        assert plant.id == SETTLEMENT.id
        return month_data(plant, start, end)


def link_settlement(monkeypatch: pytest.MonkeyPatch) -> EpiasConnection:
    system, plant = make_plant(monkeypatch)
    return connect_epias(
        plant.pk,
        actor=system.admin,
        provider_plant_id=PROVIDER_PLANT.id,
        expected_eic=cast(str, PROVIDER_PLANT.eic),
        client=cast(EpiasClient, SettlementClient()),
    ).connection
