import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.utils import timezone

from pyracast.epias import EpiasClient, EpiasDay, EpiasPlant, EpiasReading
from pyracast.metering.models import EpiasConnection
from pyracast.metering.operations import (
    connect_epias,
)
from pyracast.plants.models import Plant
from tests.support.domain import NOW, System
from tests.support.plant_operations import make_plant

PROVIDER_PLANT = EpiasPlant(7, "40X0000000000001", "Solar plant", "Solar")


NEXT_NIGHT = NOW + timedelta(hours=11)


class FixtureClient:
    def __init__(self, action: Callable[[], None] = lambda: None) -> None:
        self.calls = 0
        self.action = action

    def list_plants(self) -> tuple[EpiasPlant, ...]:
        assert not connection.in_atomic_block
        self.calls += 1
        self.action()
        return (PROVIDER_PLANT,)

    def resolve_settlement_plant(self, eic: str) -> EpiasPlant | None:
        return None

    def list_settlement_plants(self) -> tuple[EpiasPlant, ...]:
        assert not connection.in_atomic_block
        return ()

    def fetch_day(self, plant: EpiasPlant, day: date, *, deadline: float | None = None) -> EpiasDay:
        assert not connection.in_atomic_block
        return day_data(plant, day)


def day_data(plant: EpiasPlant, day: date) -> EpiasDay:
    timestamp = datetime.combine(day, datetime.min.time(), tzinfo=timezone.get_current_timezone())
    return EpiasDay(
        plant,
        day,
        tuple(
            EpiasReading(timestamp + timedelta(hours=i), f"{i:02}:00", Decimal("1.5"), None)
            for i in range(24)
        ),
        NOW,
        json.dumps({"items": [{"total": "1.5", "sun": None}]}),
    )


def connect(plant: Plant, actor: User, client: FixtureClient | None = None) -> EpiasConnection:
    assert PROVIDER_PLANT.eic is not None
    return connect_epias(
        plant.pk,
        actor=actor,
        provider_plant_id=7,
        expected_eic=PROVIDER_PLANT.eic,
        client=cast(EpiasClient, client or FixtureClient()),
    )


def unconnected(monkeypatch: pytest.MonkeyPatch) -> tuple[System, Plant]:
    monkeypatch.setattr("pyracast.metering.scheduler.get_epias_client", lambda: FixtureClient())
    return make_plant(monkeypatch)


def connected(monkeypatch: pytest.MonkeyPatch) -> tuple[System, Plant, EpiasConnection]:
    system, plant = unconnected(monkeypatch)
    return system, plant, connect(plant, system.admin)
