import json
from collections.abc import Callable
from io import StringIO
from typing import Any
from uuid import uuid4

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.incidents.models import PlantIncident
from pyracast.incidents.operations import save_incident
from tests.support.change_request_boundaries import (
    RequestCase,
    incident_values,
    make_request_case,
)
from tests.support.domain import NOW
from tests.support.openapi import validate

pytestmark = pytest.mark.django_db(transaction=True)

EDITS: dict[str, tuple[str, str, Callable[[], dict[str, Any]]]] = {
    "update": ("put", "", lambda: {**_wire(incident_values()), "title": "Stale title"}),
    "resolve": ("post", "resolve/", lambda: {"ends_at": NOW.isoformat()}),
    "cancel": ("post", "cancel/", lambda: {}),
}


def _wire(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.isoformat() if hasattr(value, "isoformat") else value
        for key, value in values.items()
    }


def _client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _outages(case: RequestCase) -> str:
    system = case.system
    return (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}"
        f"/plants/{case.plant.pk}/outages/"
    )


def _changed_meanwhile(case: RequestCase) -> tuple[PlantIncident, Client]:
    incident = save_incident(*case.scope(case.system.member), **incident_values())
    first = _client(case.system.member)
    response = first.put(
        f"{_outages(case)}{incident.pk}/",
        {**_wire(incident_values()), "title": "Changed meanwhile", "expected_revision": 1},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return incident, _client(case.system.member)


def _stale(client: Client, case: RequestCase, incident: PlantIncident, edit: str) -> Any:
    method, suffix, body = EDITS[edit]
    return getattr(client, method)(
        f"{_outages(case)}{incident.pk}/{suffix}",
        {**body(), "expected_revision": 1},
        content_type="application/json",
    )


@pytest.mark.parametrize("edit", EDITS)
def test_a_stale_outage_edit_is_refused_as_a_revision_conflict(
    monkeypatch: pytest.MonkeyPatch, edit: str
) -> None:
    case = make_request_case(monkeypatch)
    incident, second = _changed_meanwhile(case)
    response = _stale(second, case, incident, edit)
    assert response.status_code == 409, response.content
    assert response.json()["code"] == "incident_revision_changed"
    incident.refresh_from_db()
    assert (incident.revision, incident.title, incident.ends_at, incident.cancelled_at) == (
        2,
        "Changed meanwhile",
        None,
        None,
    )


@pytest.mark.parametrize("edit", EDITS)
def test_a_stale_outage_edit_answers_with_the_current_version(
    monkeypatch: pytest.MonkeyPatch, edit: str
) -> None:
    case = make_request_case(monkeypatch)
    incident, second = _changed_meanwhile(case)
    body = _stale(second, case, incident, edit).json()
    assert body["current"] == second.get(_outages(case)).json()["results"][0]
    assert body["current"]["revision"] == 2
    assert validate("IncidentConflict", body) == []


@pytest.mark.parametrize("edit", EDITS)
def test_an_outage_edit_without_its_version_is_refused_before_any_change(
    monkeypatch: pytest.MonkeyPatch, edit: str
) -> None:
    case = make_request_case(monkeypatch)
    incident = save_incident(*case.scope(case.system.member), **incident_values())
    method, suffix, body = EDITS[edit]
    response = getattr(_client(case.system.member), method)(
        f"{_outages(case)}{incident.pk}/{suffix}", body(), content_type="application/json"
    )
    assert response.status_code == 400, response.content
    assert set(response.json()["fields"]) == {"expected_revision"}
    assert "current" not in response.json()
    incident.refresh_from_db()
    assert incident.revision == 1


@pytest.mark.parametrize("edit", EDITS)
def test_a_viewer_and_an_outsider_learn_nothing_from_a_stale_outage_edit(
    monkeypatch: pytest.MonkeyPatch, edit: str
) -> None:
    case = make_request_case(monkeypatch)
    incident, _ = _changed_meanwhile(case)
    outsider = User.objects.create_user(username="outsider")
    Membership.objects.create(
        user=outsider, tenant=Tenant.objects.create(name="Other"), role=WorkspaceRole.ADMIN
    )
    for user, status in ((case.proposer, 403), (outsider, 403)):
        response = _stale(_client(user), case, incident, edit)
        assert response.status_code == status, response.content
        assert "current" not in response.json()
        assert b"Changed meanwhile" not in response.content
    missing = _client(case.system.member).put(
        f"{_outages(case)}{incident.pk + 1000}/",
        {**_wire(incident_values()), "expected_revision": 1},
        content_type="application/json",
    )
    assert missing.status_code == 404
    assert "current" not in missing.json()


def test_a_stale_outage_proposal_answers_with_the_current_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    incident, _ = _changed_meanwhile(case)
    viewer = _client(case.proposer)
    system = case.system
    response = viewer.post(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/"
        f"plants/{case.plant.pk}/change-requests/incidents/",
        {
            "action": "update",
            "idempotency_key": str(uuid4()),
            "incident_id": incident.pk,
            "expected_revision": 1,
            "values": _wire(incident_values()),
        },
        content_type="application/json",
    )
    assert response.status_code == 409, response.content
    assert response.json()["current"] == viewer.get(_outages(case)).json()["results"][0]


def _schema() -> dict[str, Any]:
    output = StringIO()
    call_command("spectacular", format="openapi-json", stdout=output)
    document: dict[str, Any] = json.loads(output.getvalue())
    return document


def test_outage_status_and_revision_conflicts_are_typed_in_the_schema() -> None:
    document = _schema()
    schemas = document["components"]["schemas"]
    status = json.dumps(schemas["IncidentOutput"]["properties"]["status"])
    assert "#/components/schemas/IncidentStatusEnum" in status
    assert schemas["IncidentStatusEnum"]["enum"] == ["active", "resolved", "cancelled"]
    plant = "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/"
    for path, method in (
        (f"{plant}outages/{{incident_id}}/", "put"),
        (f"{plant}outages/{{incident_id}}/resolve/", "post"),
        (f"{plant}outages/{{incident_id}}/cancel/", "post"),
        (f"{plant}change-requests/incidents/", "post"),
    ):
        conflict = document["paths"][path][method]["responses"]["409"]
        assert conflict["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/IncidentConflict"
        }
    assert sorted(schemas["IncidentConflict"]["required"]) == ["code", "current", "message"]
