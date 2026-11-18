from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import Client
from django.utils import timezone

from pyracast.incidents.models import IncidentRevision, PlantIncident
from pyracast.incidents.operations import pinned_incidents, save_incident, select_incidents
from pyracast.plants.models import Plant
from pyracast.plants.operations import create_plant, revise_plant
from pyracast.trash import RETENTION, purge_expired, restore_region, trash_region
from tests.support.domain import make_system
from tests.support.trash_operations import sibling

pytestmark = pytest.mark.django_db(transaction=True)


def test_incident_api_revision_and_historical_evidence() -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="PV", dc_capacity_mw=10
    )
    client = Client()
    client.force_login(system.member)
    url = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/outages/"
    start = timezone.now() - timedelta(hours=2)
    payload = {
        "kind": "inverter",
        "title": "Inverter",
        "starts_at": start.isoformat(),
        "available_dc_mw": 0,
    }
    response = client.post(url, payload, content_type="application/json")
    assert response.status_code == 201, response.content
    incident = response.json()
    assert incident["status"] == "active"
    assert incident["available_dc_mw"] == 0
    first = IncidentRevision.objects.get(incident_id=incident["id"])
    cutoff = first.created_at
    response = client.post(
        f"{url}{incident['id']}/resolve/",
        {"expected_revision": 1, "ends_at": (start + timedelta(hours=1)).isoformat()},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "resolved"
    assert (
        client.post(
            f"{url}{incident['id']}/resolve/",
            {"expected_revision": 2, "ends_at": (start + timedelta(hours=1)).isoformat()},
            content_type="application/json",
        ).status_code
        == 400
    )
    stale = client.post(
        f"{url}{incident['id']}/cancel/", {"expected_revision": 1}, content_type="application/json"
    )
    assert stale.status_code == 409
    historical = select_incidents(plant.pk, start, start + timedelta(hours=2), cutoff)
    assert len(historical) == 1
    assert historical[0].revision_id == first.pk
    assert historical[0].ends_at is None
    assert pinned_incidents(plant.pk, [first.pk]) == historical
    with pytest.raises(ValueError, match="missing"):
        pinned_incidents(plant.pk + 10000, [first.pk])
    assert client.get(url).json()["results"][0]["revision"] == 2
    cancelled = client.post(
        f"{url}{incident['id']}/cancel/",
        {"expected_revision": 2},
        content_type="application/json",
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert pinned_incidents(plant.pk, [first.pk]) == historical
    assert select_incidents(plant.pk, start, start + timedelta(hours=2), timezone.now()) == ()
    outsider = User.objects.create_user(username="outsider")
    client.force_login(outsider)
    assert client.get(url).status_code == 403


def test_validation_and_overlap() -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="PV", dc_capacity_mw=10
    )
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    start = timezone.now() - timedelta(hours=3)
    data: dict[str, Any] = dict(
        kind="other",
        title="Loss",
        notes="",
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        available_dc_mw=None,
        available_inverter_mw=None,
        available_export_mw=None,
    )
    save_incident(*scope, **data)
    with pytest.raises(ValidationError, match="overlap"):
        save_incident(
            *scope, **(data | {"starts_at": start + timedelta(minutes=30), "ends_at": None})
        )
    save_incident(*scope, **(data | {"starts_at": start + timedelta(hours=1), "ends_at": None}))
    with pytest.raises(ValidationError):
        save_incident(*scope, **(data | {"available_dc_mw": 11}))
    for invalid in (
        {"kind": "bad"},
        {"title": " "},
        {"available_dc_mw": float("nan")},
        {"available_dc_mw": True},
        {"starts_at": timezone.now() + timedelta(days=1)},
        {"cancelled_at": timezone.now()},
    ):
        with pytest.raises(ValidationError):
            save_incident(*scope, **(data | invalid))


def test_concurrent_overlaps_are_serialized() -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="PV", dc_capacity_mw=10
    )
    barrier = Barrier(2)
    start = timezone.now() - timedelta(hours=1)

    def create() -> str:
        close_old_connections()
        barrier.wait()
        try:
            save_incident(
                system.member,
                system.tenant.pk,
                system.region.pk,
                plant.pk,
                kind="other",
                title="Loss",
                notes="",
                starts_at=start,
                ends_at=None,
                available_dc_mw=None,
                available_inverter_mw=None,
                available_export_mw=None,
            )
            return "created"
        except ValidationError:
            return "overlap"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: create(), range(2))) == ["created", "overlap"]


def test_incident_updates_and_validation() -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="PV", dc_capacity_mw=10
    )
    client = Client()
    client.force_login(system.member)
    url = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/outages/"
    start = timezone.now() - timedelta(hours=3)
    payload = {"kind": "panels", "title": "Panels", "starts_at": start.isoformat()}
    created = client.post(url, payload, content_type="application/json").json()
    detail = f"{url}{created['id']}/"
    updated = client.put(
        detail,
        payload | {"expected_revision": 1, "notes": "checked"},
        content_type="application/json",
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert (
        client.put(
            detail, payload | {"expected_revision": 1}, content_type="application/json"
        ).status_code
        == 409
    )
    assert (
        client.post(
            detail + "resolve/",
            {"expected_revision": 2, "ends_at": start.isoformat()},
            content_type="application/json",
        ).status_code
        == 400
    )
    assert (
        client.post(
            detail + "cancel/", {"expected_revision": 2}, content_type="application/json"
        ).status_code
        == 200
    )
    assert (
        client.put(
            detail, payload | {"expected_revision": 3}, content_type="application/json"
        ).status_code
        == 400
    )
    assert (
        client.post(
            detail + "cancel/", {"expected_revision": 3}, content_type="application/json"
        ).status_code
        == 400
    )
    for invalid in (
        {"starts_at": "2026-01-01T00:00:00"},
        {"starts_at": (timezone.now() + timedelta(days=1)).isoformat()},
        {"ends_at": (start - timedelta(hours=1)).isoformat()},
        {"kind": "unknown"},
        {"title": ""},
        {"available_dc_mw": 11},
        {"available_dc_mw": -1},
    ):
        response = client.post(url, payload | invalid, content_type="application/json")
        assert response.status_code == 400, response.content
    with pytest.raises(ValueError, match="aware"):
        select_incidents(plant.pk, start, start, timezone.now())
    Plant.objects.filter(pk=plant.pk).update(current_configuration=None)
    assert client.post(url, payload, content_type="application/json").status_code == 400


def test_active_incident_reconfirmation_uses_current_configuration() -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="PV", dc_capacity_mw=10
    )
    client = Client()
    client.force_login(system.member)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    url = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/outages/"
    start = timezone.now() - timedelta(hours=3)
    payload = {
        "kind": "panels",
        "title": "Panels",
        "starts_at": start.isoformat(),
        "available_dc_mw": 8,
    }
    initial = client.post(url, payload, content_type="application/json").json()
    first_revision = IncidentRevision.objects.get(incident_id=initial["id"])
    revised = revise_plant(*scope, name="PV", dc_capacity_mw=5)
    detail = f"{url}{initial['id']}/"
    rejected = client.put(
        detail, payload | {"expected_revision": 1}, content_type="application/json"
    )
    assert rejected.status_code == 400
    assert client.get(url).json()["results"][0]["revision"] == 1
    accepted = client.put(
        detail,
        payload | {"expected_revision": 1, "available_dc_mw": 4},
        content_type="application/json",
    )
    assert accepted.status_code == 200
    assert accepted.json()["declared_configuration_id"] == revised.current_configuration_id
    assert (
        pinned_incidents(plant.pk, [first_revision.pk])[0].declared_configuration_id
        == initial["declared_configuration_id"]
    )
    resolved = client.post(
        detail + "resolve/",
        {"expected_revision": 2, "ends_at": (start + timedelta(hours=1)).isoformat()},
        content_type="application/json",
    )
    assert resolved.status_code == 200
    revise_plant(*scope, name="PV", dc_capacity_mw=3)
    corrected = client.put(
        detail,
        payload
        | {
            "expected_revision": 3,
            "available_dc_mw": 4,
            "ends_at": (start + timedelta(hours=1)).isoformat(),
        },
        content_type="application/json",
    )
    assert corrected.status_code == 200
    assert corrected.json()["declared_configuration_id"] == revised.current_configuration_id


def test_incident_history_survives_restore_and_is_purged_with_plant() -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="PV", dc_capacity_mw=10
    )
    other_region, _ = sibling(system)
    other = create_plant(
        system.member, system.tenant.pk, other_region.pk, name="Other", dc_capacity_mw=10
    )
    parent = (system.member, system.tenant.pk, system.region.pk)
    start = timezone.now() - timedelta(hours=2)
    values: dict[str, Any] = dict(
        kind="other",
        title="Loss",
        notes="",
        starts_at=start,
        ends_at=None,
        available_dc_mw=5,
        available_inverter_mw=None,
        available_export_mw=None,
    )
    incident = save_incident(*parent, plant.pk, **values)
    save_incident(*parent, plant.pk, incident_id=incident.pk, expected_revision=1, **values)
    other_incident = save_incident(
        system.member, system.tenant.pk, other_region.pk, other.pk, **values
    )
    revision_ids = list(
        IncidentRevision.objects.filter(incident=incident).values_list("pk", flat=True)
    )
    trash_region(*parent)
    restore_region(*parent)
    assert len(pinned_incidents(plant.pk, revision_ids)) == 2
    trash_region(*parent)
    assert purge_expired(timezone.now() + RETENTION + timedelta(seconds=1)) == {
        "regions": 1,
        "plants": 1,
    }
    assert not Plant.objects.filter(pk=plant.pk).exists()
    assert not PlantIncident.objects.filter(pk=incident.pk).exists()
    assert not IncidentRevision.objects.filter(pk__in=revision_ids).exists()
    assert PlantIncident.objects.filter(pk=other_incident.pk).exists()
    assert IncidentRevision.objects.filter(incident=other_incident).exists()


def test_active_incident_update_requires_current_configuration() -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="PV", dc_capacity_mw=10
    )
    parent = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    values: dict[str, Any] = dict(
        kind="other",
        title="Loss",
        notes="",
        starts_at=timezone.now() - timedelta(hours=1),
        ends_at=None,
        available_dc_mw=None,
        available_inverter_mw=None,
        available_export_mw=None,
    )
    incident = save_incident(*parent, **values)
    Plant.objects.filter(pk=plant.pk).update(current_configuration=None)
    with pytest.raises(ValidationError, match="configuration"):
        save_incident(*parent, incident_id=incident.pk, expected_revision=1, **values)
    assert IncidentRevision.objects.filter(incident=incident).count() == 1
