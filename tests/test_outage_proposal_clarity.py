from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.contrib.auth.models import User
from django.test import Client

from pyracast.accounts.models import Membership
from pyracast.change_requests.models import ChangeRequest, ChangeRequestStatus
from pyracast.change_requests.operations import reject_request, submit_incident
from pyracast.change_requests.policy import PENDING_LIMIT
from pyracast.incidents.models import PlantIncident
from pyracast.incidents.operations import save_incident
from pyracast.plants.models import Plant, PlantConfiguration
from pyracast.plants.operations import create_plant
from tests.support.budgets import captured_statements
from tests.support.change_request_boundaries import (
    RequestCase,
    incident_values,
    make_request_case,
    production_request,
)
from tests.support.domain import NOW, System
from tests.support.notification_groups import second_workspace

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])

WRITES = ("INSERT", "UPDATE", "DELETE")


def _client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _plant_path(system: System, plant: Plant) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/"


def _outages(case: RequestCase) -> str:
    return _plant_path(case.system, case.plant) + "outages/"


def _wire(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.isoformat() if hasattr(value, "isoformat") else value
        for key, value in values.items()
    }


def _outage(system: System, plant: Plant) -> PlantIncident:
    return save_incident(
        system.member, system.tenant.pk, system.region.pk, plant.pk, **incident_values()
    )


def _propose_update(actor: User, system: System, plant: Plant, incident: int) -> ChangeRequest:
    return submit_incident(
        actor,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        action="update",
        idempotency_key=uuid4(),
        incident_id=incident,
        expected_revision=1,
        values={**incident_values(), "available_dc_mw": 0},
    )


def _changed_meanwhile(case: RequestCase, incident: PlantIncident) -> None:
    save_incident(
        *case.scope(case.system.member),
        incident_id=incident.pk,
        expected_revision=1,
        **{**incident_values(), "title": "Changed meanwhile"},
    )


def _stale_proposal(client: Client, case: RequestCase, incident: PlantIncident) -> Any:
    return client.post(
        _plant_path(case.system, case.plant) + "change-requests/incidents/",
        {
            "action": "update",
            "idempotency_key": str(uuid4()),
            "incident_id": incident.pk,
            "expected_revision": 1,
            "values": _wire(incident_values()),
        },
        content_type="application/json",
    )


def _stale_edit(client: Client, case: RequestCase, incident: PlantIncident) -> Any:
    return client.put(
        f"{_outages(case)}{incident.pk}/",
        {**_wire(incident_values()), "title": "Stale title", "expected_revision": 1},
        content_type="application/json",
    )


def _other_workspace(case: RequestCase) -> tuple[System, Plant]:
    other = second_workspace(case.system)
    plant = create_plant(
        other.member,
        other.tenant.pk,
        other.region.pk,
        name="Other plant",
        dc_capacity_mw=130,
        export_limit_mw=100,
    )
    assert plant.current_configuration_id is not None
    PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(
        created_at=NOW - timedelta(days=100)
    )
    plant.refresh_from_db()
    Membership.objects.create(user=case.proposer, tenant=other.tenant)
    return other, plant


def test_a_conflict_answer_names_the_members_own_pending_request_as_the_list_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    incident = _outage(case.system, case.plant)
    own = _propose_update(case.proposer, case.system, case.plant, incident.pk)
    _changed_meanwhile(case, incident)
    member = _client(case.proposer)

    response = _stale_proposal(member, case, incident)

    assert response.status_code == 409, response.content
    current = response.json()["current"]
    assert current["pending_request_id"] == own.pk
    assert current == member.get(_outages(case)).json()["results"][0]


def test_a_conflict_answer_never_names_another_members_or_another_workspaces_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    incident = _outage(case.system, case.plant)
    colleague = User.objects.create_user(username="colleague")
    Membership.objects.create(user=colleague, tenant=case.system.tenant)
    _propose_update(colleague, case.system, case.plant, incident.pk)
    other, other_plant = _other_workspace(case)
    elsewhere = _outage(other, other_plant)
    _propose_update(case.proposer, other, other_plant, elsewhere.pk)
    _changed_meanwhile(case, incident)

    member = _stale_proposal(_client(case.proposer), case, incident)
    administrator = _stale_edit(_client(case.system.member), case, incident)

    assert (member.status_code, administrator.status_code) == (409, 409)
    assert member.json()["current"]["pending_request_id"] is None
    assert administrator.json()["current"]["pending_request_id"] is None


def test_a_member_cannot_propose_an_outage_for_a_plant_without_a_current_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    Plant.objects.filter(pk=case.plant.pk).update(current_configuration=None)

    response = _client(case.proposer).post(
        _plant_path(case.system, case.plant) + "change-requests/incidents/",
        {
            "action": "create",
            "idempotency_key": str(uuid4()),
            "values": _wire(incident_values()),
        },
        content_type="application/json",
    )

    assert response.status_code == 400, response.content
    assert response.json()["code"] == "configuration_unavailable"
    assert not ChangeRequest.objects.exists()


STATES = {
    "member": None,
    "member without a current configuration": "configuration_unavailable",
    "member at the pending limit": "pending_request_limit",
    "administrator": "admin_edits_directly",
}


def _arrange(case: RequestCase, state: str) -> User:
    if state == "administrator":
        return case.system.member
    if state == "member without a current configuration":
        Plant.objects.filter(pk=case.plant.pk).update(current_configuration=None)
    if state == "member at the pending limit":
        for _ in range(PENDING_LIMIT):
            submit_incident(
                *case.scope(case.proposer),
                action="create",
                idempotency_key=uuid4(),
                values=incident_values(),
            )
    return case.proposer


@pytest.mark.parametrize("state", STATES)
def test_the_outage_list_offers_a_new_proposal_exactly_when_submission_accepts_it(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    case = make_request_case(monkeypatch)
    client = _client(_arrange(case, state))

    listing = client.get(_outages(case))
    response = client.post(
        _plant_path(case.system, case.plant) + "change-requests/incidents/",
        {
            "action": "create",
            "idempotency_key": str(uuid4()),
            "values": _wire(incident_values()),
        },
        content_type="application/json",
    )

    assert listing.status_code == 200, listing.content
    offered = listing.json()
    accepted = response.status_code == 201
    assert (offered["can_propose"], offered["proposal_unavailable_reason"]) == (
        STATES[state] is None,
        STATES[state],
    )
    assert accepted == offered["can_propose"], response.content
    assert accepted or response.json()["code"] == offered["proposal_unavailable_reason"]


def test_a_new_outage_both_invalid_and_over_the_limit_is_refused_for_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    client = _client(_arrange(case, "member at the pending limit"))

    response = client.post(
        _plant_path(case.system, case.plant) + "change-requests/incidents/",
        {
            "action": "create",
            "idempotency_key": str(uuid4()),
            "values": _wire({**incident_values(), "starts_at": NOW + timedelta(hours=1)}),
        },
        content_type="application/json",
    )

    assert response.status_code == 409, response.content
    assert response.json()["code"] == "pending_request_limit"
    assert ChangeRequest.objects.count() == PENDING_LIMIT


@pytest.mark.parametrize("action", ["create", "update"])
def test_an_outage_proposal_counts_the_members_pending_requests_once(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    case = make_request_case(monkeypatch)
    existing = _outage(case.system, case.plant).pk if action == "update" else None

    with captured_statements() as statements:
        submit_incident(
            *case.scope(case.proposer),
            action=action,
            idempotency_key=uuid4(),
            incident_id=existing,
            expected_revision=None if existing is None else 1,
            values={**incident_values(), "available_dc_mw": 0},
        )

    counts = [
        statement
        for statement in statements
        if "COUNT(" in statement.upper() and '"change_requests_changerequest"' in statement
    ]
    assert len(counts) == 1, statements


def test_the_outage_proposal_flag_never_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    case = make_request_case(monkeypatch)
    client = _client(case.proposer)
    client.get(_outages(case))

    with captured_statements() as statements:
        assert client.get(_outages(case)).json()["can_propose"] is True

    assert not [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(WRITES) or "FOR UPDATE" in statement
    ]


def test_reading_a_decided_request_never_marks_its_outcome_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    proposal = production_request(case)
    reject_request(
        case.system.member, case.system.tenant.pk, proposal.pk, reason="Meter reading differs"
    )
    client = _client(case.proposer)
    inbox = f"/api/tenants/{case.system.tenant.pk}/change-requests/"
    client.get(case.detail_path(proposal))

    with captured_statements() as statements:
        detail = client.get(case.detail_path(proposal)).json()
        listed = client.get(inbox, {"scope": "mine"}).json()["results"]
        workspaces = client.get("/api/tenants/").json()["results"]

    assert detail["unread"] is True
    assert [row["unread"] for row in listed] == [True]
    assert [row["unread_outcome_count"] for row in workspaces] == [1]
    assert not [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(WRITES) or "FOR UPDATE" in statement
    ]
    proposal.refresh_from_db()
    assert proposal.outcome_seen_at is None


def _pending_ids() -> set[int]:
    return set(
        ChangeRequest.objects.filter(status=ChangeRequestStatus.PENDING).values_list(
            "pk", flat=True
        )
    )


def test_a_second_interval_correction_keeps_both_pending_and_the_marker_names_the_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    first = production_request(case, energy=1000)
    latest = production_request(case, energy=2000)

    response = _client(case.proposer).get(
        _plant_path(case.system, case.plant) + "production/",
        {
            "start_date": NOW.date().isoformat(),
            "end_date": NOW.date().isoformat(),
            "native_intervals": "true",
        },
    )

    assert response.status_code == 200, response.content
    assert _pending_ids() == {first.pk, latest.pk}
    assert {row["pending_request_id"] for row in response.json()["intervals"]} - {None} == {
        latest.pk
    }


def test_a_second_outage_proposal_keeps_both_pending_and_the_marker_names_the_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    incident = _outage(case.system, case.plant)
    first = _propose_update(case.proposer, case.system, case.plant, incident.pk)
    latest = _propose_update(case.proposer, case.system, case.plant, incident.pk)

    rows = _client(case.proposer).get(_outages(case)).json()["results"]

    assert _pending_ids() == {first.pk, latest.pk}
    assert [row["pending_request_id"] for row in rows] == [latest.pk]
