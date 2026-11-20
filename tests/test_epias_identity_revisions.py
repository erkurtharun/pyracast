from datetime import datetime, timedelta
from typing import Any, cast

import pytest
from django.apps import apps
from django.db import IntegrityError, transaction
from django.utils import timezone

from pyracast.epias import EpiasClient, EpiasPlant
from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection
from pyracast.metering.operations import connect_epias, disconnect_epias
from pyracast.plants.measurements import production_identity_at
from pyracast.plants.models import CalibrationRun, Plant, PlantEvent
from tests.support.domain import NOW
from tests.support.metering import PROVIDER_PLANT, FixtureClient, connect, connected
from tests.support.plant_operations import make_plant
from tests.support.provisional_measurements import acquisition

pytestmark = pytest.mark.django_db(transaction=True)

RENAMED = EpiasPlant(8, "40X0000000000002", "Renamed plant", "Renamed")


def revisions() -> Any:
    return apps.get_model("metering", "EpiasConnectionRevision").objects


class TwoPlantsClient(FixtureClient):
    def list_plants(self) -> tuple[EpiasPlant, ...]:
        return (PROVIDER_PLANT, RENAMED)


def identities(plant: Plant) -> list[tuple[int, int, str]]:
    return list(
        revisions()
        .filter(connection__plant=plant)
        .order_by("revision")
        .values_list("revision", "provider_plant_id", "eic")
    )


def test_connect_records_the_identity_revision_with_its_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plant, linked = connected(monkeypatch)

    revision = revisions().get()
    event = PlantEvent.objects.get(plant=plant, action="epias_connected")
    assert (revision.connection_id, revision.connected_at) == (linked.pk, event.created_at)
    assert identities(plant) == [(1, PROVIDER_PLANT.id, PROVIDER_PLANT.eic)]


def test_a_failed_event_write_leaves_no_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("event store unavailable")

    monkeypatch.setattr("pyracast.metering.operations.record_plant_event", fail)
    with pytest.raises(RuntimeError, match="event store unavailable"):
        connect(plant, system.admin)

    assert not revisions().exists()
    assert not EpiasConnection.objects.exists()


def test_an_identical_reconnect_writes_nothing_and_queues_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, linked = connected(monkeypatch)
    jobs = set(EpiasCollectionJob.objects.values_list("pk", "status"))
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(hours=1))

    again = connect(plant, system.admin)

    assert (again.pk, again.revision) == (linked.pk, linked.revision)
    assert PlantEvent.objects.filter(plant=plant, action="epias_connected").count() == 1
    assert set(EpiasCollectionJob.objects.values_list("pk", "status")) == jobs
    assert identities(plant) == [(1, PROVIDER_PLANT.id, PROVIDER_PLANT.eic)]


def test_a_changed_or_restored_connection_records_a_new_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _ = connected(monkeypatch)
    client = TwoPlantsClient()
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(hours=1))
    assert RENAMED.eic is not None

    connect_epias(
        plant.pk,
        actor=system.admin,
        provider_plant_id=RENAMED.id,
        expected_eic=RENAMED.eic,
        client=cast(EpiasClient, client),
    )
    disconnect_epias(plant.pk, actor=system.admin)
    disconnect_epias(plant.pk, actor=system.admin)
    connect(plant, system.admin)

    assert identities(plant) == [
        (1, PROVIDER_PLANT.id, PROVIDER_PLANT.eic),
        (2, RENAMED.id, RENAMED.eic),
        (3, PROVIDER_PLANT.id, PROVIDER_PLANT.eic),
    ]
    assert PlantEvent.objects.filter(plant=plant, action="epias_disconnected").count() == 1
    assert PlantEvent.objects.filter(plant=plant, action="epias_connected").count() == 3


def test_a_revision_holds_only_a_usable_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, linked = connected(monkeypatch)
    for values in (
        {"revision": 0},
        {"provider_plant_id": 0},
        {"eic": ""},
        {"revision": 1},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            revisions().create(
                connection=linked,
                **{"revision": 2, "provider_plant_id": 7, "eic": "EIC", "connected_at": NOW}
                | values,
            )


def day_end(plant: Plant) -> datetime:
    configuration = plant.current_configuration
    assert configuration is not None
    _, end = local_day_bounds(NOW.date(), configuration.binding.timezone)
    return end - timedelta(microseconds=1)


def test_historical_identity_is_read_from_the_connection_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    revisions().create(
        connection=connection, revision=1, provider_plant_id=2, eic="EIC", connected_at=NOW
    )
    snapshot = acquisition((configuration, connection))
    run = CalibrationRun(
        plant=plant, configuration=configuration, production_snapshot_ids=[snapshot.pk]
    )
    cutoff = day_end(plant)

    assert not PlantEvent.objects.filter(action="epias_connected").exists()
    assert production_identity_at(run, cutoff)
    revisions().create(
        connection=connection,
        revision=2,
        provider_plant_id=3,
        eic="EIC",
        connected_at=cutoff + timedelta(minutes=30),
    )
    assert production_identity_at(run, cutoff)
    revisions().filter(revision=2).update(connected_at=cutoff - timedelta(minutes=30))
    assert not production_identity_at(run, cutoff)
    revisions().all().delete()
    assert not production_identity_at(run, cutoff)
