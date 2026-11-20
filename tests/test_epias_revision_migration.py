import importlib
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.apps import apps
from django.db import connection
from django.utils import timezone

from pyracast.metering.models import EpiasConnection
from pyracast.plants.measurements import production_identity_at
from pyracast.plants.models import CalibrationRun, Plant, PlantEvent
from pyracast.regions.models import Region
from tests.support.domain import NOW
from tests.support.migrations import forward, migrate
from tests.support.plant_operations import make_plant
from tests.support.provisional_measurements import acquisition
from tests.support.trash_operations import sibling_plant

pytestmark = pytest.mark.django_db(transaction=True)

MIGRATION = "pyracast.metering.migrations.0005_epias_connection_revisions"


def event(
    monkeypatch: pytest.MonkeyPatch, plant: Plant, at: datetime, details: dict[str, object]
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: at)
    PlantEvent.objects.create(plant=plant, action="epias_connected", details=details)


def stored() -> list[tuple[int, int, int, str, datetime]]:
    return list(
        apps.get_model("metering", "EpiasConnectionRevision")
        .objects.order_by("connection_id", "revision")
        .values_list("connection_id", "revision", "provider_plant_id", "eic", "connected_at")
    )


def test_the_revision_table_replays_every_connection_event_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    system, reconnected = make_plant(monkeypatch)
    configuration = reconnected.current_configuration
    assert configuration is not None
    other = sibling_plant(system)
    unconnected = Plant.objects.create(
        region=Region.objects.create(
            tenant=system.tenant,
            name="Unconnected",
            latitude=38,
            longitude=27,
            timezone="Europe/Istanbul",
        ),
        name="Unconnected",
    )
    first = EpiasConnection.objects.create(
        plant=reconnected,
        provider_plant_id=3,
        eic="EIC",
        name="Plant",
        revision=3,
        connected_by=system.admin,
    )
    second = EpiasConnection.objects.create(
        plant=other, provider_plant_id=9, eic="OTHER", name="Other", connected_by=system.admin
    )
    moments = [NOW - timedelta(days=10), NOW - timedelta(days=5), NOW + timedelta(hours=2)]
    event(
        monkeypatch, reconnected, moments[0], {"provider_plant_id": 2, "eic": "EIC", "revision": 1}
    )
    monkeypatch.setattr(timezone, "now", lambda: moments[0] + timedelta(days=1))
    PlantEvent.objects.create(
        plant=reconnected,
        action="epias_disconnected",
        details={"provider_plant_id": 2, "eic": "EIC", "revision": 1},
    )
    event(
        monkeypatch, reconnected, moments[1], {"provider_plant_id": 2, "eic": "EIC", "revision": 2}
    )
    event(
        monkeypatch, reconnected, moments[2], {"provider_plant_id": 3, "eic": "EIC", "revision": 3}
    )
    event(monkeypatch, other, moments[0], {"provider_plant_id": 9, "eic": "OTHER", "revision": 1})
    event(monkeypatch, other, moments[1], {"provider_plant_id": 9, "eic": "OTHER", "revision": 1})
    for malformed in (
        {},
        {"provider_plant_id": 2, "eic": "EIC", "revision": "4"},
        {"provider_plant_id": True, "eic": "EIC", "revision": 4},
        {"provider_plant_id": 2, "eic": "", "revision": 4},
        {"provider_plant_id": 10, "eic": "OTHER", "revision": 1},
        {"provider_plant_id": 2, "eic": "EIC", "revision": 2**31},
        {"provider_plant_id": 2**63, "eic": "EIC", "revision": 4},
    ):
        event(
            monkeypatch, other if malformed.get("eic") == "OTHER" else reconnected, NOW, malformed
        )
    event(monkeypatch, unconnected, NOW, {"provider_plant_id": 5, "eic": "EIC", "revision": 1})
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    snapshot = acquisition((configuration, first))

    migrate(("metering", "0004_schema_naming_convention"))
    capsys.readouterr()
    forward()

    expected = [
        (first.pk, 1, 2, "EIC", moments[0]),
        (first.pk, 2, 2, "EIC", moments[1]),
        (first.pk, 3, 3, "EIC", moments[2]),
        (second.pk, 1, 9, "OTHER", moments[0]),
    ]
    assert stored() == expected
    assert "EPIAS connection revisions: 4 written, 8 skipped." in capsys.readouterr().out
    run = CalibrationRun(
        plant=reconnected, configuration=configuration, production_snapshot_ids=[snapshot.pk]
    )
    assert production_identity_at(run, NOW + timedelta(hours=1))
    assert not production_identity_at(run, NOW + timedelta(hours=3))

    backfill: Any = importlib.import_module(MIGRATION).copy_connection_events
    backfill(apps, SimpleNamespace(connection=connection))
    assert stored() == expected
    assert "EPIAS connection revisions: 0 written, 8 skipped." in capsys.readouterr().out
