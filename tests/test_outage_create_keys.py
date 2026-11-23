import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Barrier
from typing import Any, cast
from uuid import uuid4

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership
from pyracast.accounts.operations import reassign_workspace_admin
from pyracast.incidents.models import IncidentRevision, PlantIncident
from pyracast.plants.models import Plant
from pyracast.plants.operations import create_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.plants.preparation import incidents_changed
from tests.support.domain import System, make_system
from tests.support.trash_operations import sibling_plant

pytestmark = pytest.mark.django_db(transaction=True)


def outage_plant(system: System) -> Plant:
    return create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        PlantParameters(name="PV", dc_capacity_mw=10),
    )


def outages_url(system: System, plant: Plant) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{plant.region_id}/plants/{plant.pk}/outages/"


def signed_in(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def outage(key: object = None, **fields: Any) -> dict[str, Any]:
    body = {
        "kind": "inverter",
        "title": "Inverter offline",
        "starts_at": (timezone.now() - timedelta(hours=2)).replace(microsecond=0).isoformat(),
        "available_dc_mw": 0,
        **fields,
    }
    return body if key is None else {**body, "idempotency_key": str(key)}


def counted_outage_changes(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    calls: list[int] = []

    def count(plant_id: int) -> None:
        calls.append(plant_id)
        incidents_changed(plant_id)

    monkeypatch.setattr("pyracast.incidents.operations.incidents_changed", count)
    return lambda: len(calls)


def test_a_repeated_create_with_its_key_answers_with_the_first_outage_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    plant = outage_plant(system)
    changes = counted_outage_changes(monkeypatch)
    client = signed_in(system.member)
    body = outage(uuid4())

    first = client.post(outages_url(system, plant), body, content_type="application/json")
    repeat = client.post(outages_url(system, plant), body, content_type="application/json")

    assert first.status_code == 201, first.content
    assert repeat.status_code == 201, repeat.content
    assert repeat.json() == first.json()
    assert PlantIncident.objects.filter(plant=plant).count() == 1
    assert IncidentRevision.objects.filter(incident__plant=plant).count() == 1
    assert changes() == 1


def edited(client: Client, url: str) -> dict[str, Any]:
    response = client.put(
        url,
        {**outage(), "title": "Inverter replaced", "expected_revision": 1},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return cast(dict[str, Any], response.json())


def resolved(client: Client, url: str) -> dict[str, Any]:
    ends_at = (timezone.now() - timedelta(hours=1)).replace(microsecond=0).isoformat()
    response = client.post(
        f"{url}resolve/",
        {"expected_revision": 1, "ends_at": ends_at},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return cast(dict[str, Any], response.json())


def cancelled(client: Client, url: str) -> dict[str, Any]:
    response = client.post(
        f"{url}cancel/", {"expected_revision": 1}, content_type="application/json"
    )
    assert response.status_code == 200, response.content
    return cast(dict[str, Any], response.json())


@pytest.mark.parametrize("change", [edited, resolved, cancelled])
def test_a_repeat_after_a_later_change_answers_with_the_outage_as_it_now_stands(
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[Client, str], dict[str, Any]],
) -> None:
    system = make_system(approved=True)
    plant = outage_plant(system)
    client = signed_in(system.member)
    body = outage(uuid4())
    created = client.post(outages_url(system, plant), body, content_type="application/json").json()
    current = change(client, f"{outages_url(system, plant)}{created['id']}/")
    changes = counted_outage_changes(monkeypatch)

    repeat = client.post(outages_url(system, plant), body, content_type="application/json")

    assert repeat.status_code == 201, repeat.content
    assert repeat.json() == current
    assert current["revision"] == 2
    assert PlantIncident.objects.filter(plant=plant).count() == 1
    assert IncidentRevision.objects.filter(incident__plant=plant).count() == 2
    assert changes() == 0


def test_a_key_sent_again_with_other_values_is_refused() -> None:
    system = make_system(approved=True)
    plant = outage_plant(system)
    client = signed_in(system.member)
    key = uuid4()
    body = outage(key)
    client.post(outages_url(system, plant), body, content_type="application/json")

    edited = client.post(
        outages_url(system, plant), {**body, "title": "Edited"}, content_type="application/json"
    )

    assert edited.status_code == 409, edited.content
    assert edited.json()["code"] == "idempotency_key_reused"
    assert PlantIncident.objects.get(plant=plant).title == "Inverter offline"


def test_the_direct_create_declares_its_conflict_with_the_error_body() -> None:
    output = StringIO()
    call_command("spectacular", format="openapi-json", stdout=output)
    path = "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/"
    conflict = json.loads(output.getvalue())["paths"][path]["post"]["responses"]["409"]

    assert conflict["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Error"
    }


def test_a_create_without_a_key_is_checked_for_overlap_as_before() -> None:
    system = make_system(approved=True)
    plant = outage_plant(system)
    client = signed_in(system.member)
    body = outage()

    first = client.post(outages_url(system, plant), body, content_type="application/json")
    second = client.post(outages_url(system, plant), body, content_type="application/json")

    assert first.status_code == 201, first.content
    assert second.status_code == 400, second.content
    assert second.json()["code"] == "incident_overlap"
    assert PlantIncident.objects.filter(plant=plant).count() == 1


def test_the_same_key_on_another_plant_is_another_create() -> None:
    system = make_system(approved=True)
    first_plant, second_plant = outage_plant(system), sibling_plant(system)
    client = signed_in(system.member)
    body = outage(uuid4())

    first = client.post(outages_url(system, first_plant), body, content_type="application/json")
    second = client.post(outages_url(system, second_plant), body, content_type="application/json")

    assert second.status_code == 201, second.content
    assert second.json()["id"] != first.json()["id"]
    assert PlantIncident.objects.get(pk=second.json()["id"]).plant_id == second_plant.pk


def test_the_same_key_by_another_person_is_another_create() -> None:
    system = make_system(approved=True)
    plant = outage_plant(system)
    successor = User.objects.create_user(username="successor")
    Membership.objects.create(user=successor, tenant=system.tenant)
    key = uuid4()
    first = signed_in(system.member).post(
        outages_url(system, plant),
        outage(key, ends_at=(timezone.now() - timedelta(hours=1)).isoformat()),
        content_type="application/json",
    )
    reassign_workspace_admin(
        system.admin, system.tenant.pk, successor.pk, expected_admin_id=system.member.pk
    )

    later = signed_in(successor).post(
        outages_url(system, plant),
        outage(key, starts_at=(timezone.now() - timedelta(minutes=30)).isoformat()),
        content_type="application/json",
    )

    assert first.status_code == 201, first.content
    assert later.status_code == 201, later.content
    assert later.json()["id"] != first.json()["id"]
    assert PlantIncident.objects.get(pk=later.json()["id"]).created_by == successor


def test_concurrent_creates_with_one_key_make_one_outage_and_both_answer_with_it() -> None:
    system = make_system(approved=True)
    plant = outage_plant(system)
    clients = [signed_in(system.member), signed_in(system.member)]
    body = outage(uuid4())
    barrier = Barrier(2)

    def create(client: Client) -> tuple[int, int]:
        close_old_connections()
        barrier.wait()
        try:
            response = client.post(
                outages_url(system, plant), body, content_type="application/json"
            )
            return response.status_code, response.json()["id"]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        answers = list(executor.map(create, clients))

    created = PlantIncident.objects.get(plant=plant)
    assert answers == [(201, created.pk), (201, created.pk)]
    assert IncidentRevision.objects.filter(incident=created).count() == 1
