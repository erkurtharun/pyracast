from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from django.contrib.auth.models import User
from django.test import Client

from pyracast.accounts.models import Membership
from pyracast.change_requests.models import ChangeRequest
from pyracast.change_requests.operations import submit_incident, submit_production, withdraw_request
from pyracast.incidents.operations import save_incident
from pyracast.plants.models import Plant, PlantConfiguration
from pyracast.plants.operations import create_plant
from pyracast.plants.production_changes import ProductionChange
from tests.support.budgets import assert_scale_invariant, captured_statements
from tests.support.change_request_boundaries import (
    RequestCase,
    approve,
    incident_values,
    make_request_case,
)
from tests.support.domain import NOW, System
from tests.support.notification_groups import second_workspace
from tests.support.trash_operations import sibling_plant

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])

DAY = NOW.date()


def _slot(hours_before_now: int) -> tuple[datetime, datetime]:
    start = NOW - timedelta(hours=hours_before_now)
    return start, start + timedelta(hours=1)


def _propose(
    actor: User, system: System, plant: Plant, *slots: tuple[datetime, datetime]
) -> ChangeRequest:
    assert plant.current_configuration_id is not None
    return submit_production(
        actor,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        unit="MWh",
        changes=[ProductionChange(start, end, None, "set", 1) for start, end in slots],
        reason="Verified meter reading",
        idempotency_key=uuid4(),
    )


def _signed_in(actor: User) -> Client:
    client = Client()
    client.force_login(actor)
    return client


def _history(client: Client, system: System, plant: Plant) -> dict[datetime, int | None]:
    response = client.get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/production/",
        {"start_date": DAY.isoformat(), "end_date": DAY.isoformat(), "native_intervals": "true"},
    )
    assert response.status_code == 200, response.content
    return {
        datetime.fromisoformat(row["starts_at"]): row["pending_request_id"]
        for row in response.json()["intervals"]
    }


def _marked(actor: User, system: System, plant: Plant) -> dict[datetime, int | None]:
    markers = _history(_signed_in(actor), system, plant)
    return {start: marker for start, marker in markers.items() if marker}


def _colleague(case: RequestCase) -> User:
    colleague = User.objects.create_user(username="colleague")
    Membership.objects.create(user=colleague, tenant=case.system.tenant)
    return colleague


def _other_workspace_plant(case: RequestCase) -> tuple[System, Plant]:
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


def test_history_marks_the_members_own_pending_intervals_with_their_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    first, second = _slot(3), _slot(2)
    request = _propose(case.proposer, case.system, case.plant, first, second)

    assert _marked(case.proposer, case.system, case.plant) == {
        first[0]: request.pk,
        second[0]: request.pk,
    }


def test_history_marks_the_latest_pending_request_of_an_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    slot = _slot(3)
    _propose(case.proposer, case.system, case.plant, slot)
    latest = _propose(case.proposer, case.system, case.plant, slot)

    assert _marked(case.proposer, case.system, case.plant) == {slot[0]: latest.pk}


def test_history_never_marks_another_members_or_an_administrators_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    colleague = _colleague(case)
    _propose(case.proposer, case.system, case.plant, _slot(3))

    assert _marked(colleague, case.system, case.plant) == {}
    assert _marked(case.system.member, case.system, case.plant) == {}


def test_history_never_marks_a_pending_request_of_another_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    other, other_plant = _other_workspace_plant(case)
    slot = _slot(3)
    elsewhere = _propose(case.proposer, other, other_plant, slot)
    here = _propose(case.proposer, case.system, case.plant, _slot(2))

    assert _marked(case.proposer, case.system, case.plant) == {_slot(2)[0]: here.pk}
    assert _marked(case.proposer, other, other_plant) == {slot[0]: elsewhere.pk}


def test_history_leaves_withdrawn_and_decided_requests_unmarked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    withdrawn = _propose(case.proposer, case.system, case.plant, _slot(3))
    withdraw_request(case.proposer, case.system.tenant.pk, withdrawn.pk)
    approve(case, _propose(case.proposer, case.system, case.plant, _slot(2)))

    assert _marked(case.proposer, case.system, case.plant) == {}


def test_history_with_markers_never_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    case = make_request_case(monkeypatch)
    _propose(case.proposer, case.system, case.plant, _slot(3))
    client = _signed_in(case.proposer)
    _history(client, case.system, case.plant)

    with captured_statements() as statements:
        assert any(_history(client, case.system, case.plant).values())

    assert not [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        or "FOR UPDATE" in statement
    ]


def test_history_markers_keep_a_fixed_number_of_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    case = make_request_case(monkeypatch)
    client = _signed_in(case.proposer)
    proposed = 0

    def seed(count: int) -> None:
        nonlocal proposed
        for _ in range(count):
            proposed += 1
            _propose(case.proposer, case.system, case.plant, _slot(proposed + 1))

    assert_scale_invariant(
        seed, lambda: _history(client, case.system, case.plant), small=1, large=10
    )


def _outage(case: RequestCase) -> int:
    system = case.system
    incident = save_incident(
        system.member,
        system.tenant.pk,
        system.region.pk,
        case.plant.pk,
        **incident_values(),
    )
    return incident.pk


def _propose_outage_update(case: RequestCase, actor: User, incident_id: int) -> ChangeRequest:
    return submit_incident(
        actor,
        case.system.tenant.pk,
        case.system.region.pk,
        case.plant.pk,
        action="update",
        idempotency_key=uuid4(),
        incident_id=incident_id,
        expected_revision=1,
        values={**incident_values(), "available_dc_mw": 0},
    )


def _outage_markers(actor: User, case: RequestCase) -> dict[int, int | None]:
    client = Client()
    client.force_login(actor)
    system = case.system
    response = client.get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{case.plant.pk}/outages/"
    )
    assert response.status_code == 200, response.content
    return {row["id"]: row["pending_request_id"] for row in response.json()["results"]}


def test_outage_list_marks_only_the_members_own_pending_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    colleague = _colleague(case)
    incident = _outage(case)
    request = _propose_outage_update(case, case.proposer, incident)

    assert _outage_markers(case.proposer, case) == {incident: request.pk}
    assert _outage_markers(colleague, case) == {incident: None}
    assert _outage_markers(case.system.member, case) == {incident: None}

    withdraw_request(case.proposer, case.system.tenant.pk, request.pk)
    assert _outage_markers(case.proposer, case) == {incident: None}


def test_outage_markers_keep_a_fixed_number_of_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    case = make_request_case(monkeypatch)
    system = case.system
    client = _signed_in(case.proposer)
    url = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/"
        f"plants/{case.plant.pk}/outages/"
    )
    outages = 0

    def seed(count: int) -> None:
        nonlocal outages
        for _ in range(count):
            outages += 1
            start = NOW - timedelta(hours=2 * outages + 1)
            values = {
                **incident_values(),
                "starts_at": start,
                "ends_at": start + timedelta(hours=1),
            }
            incident = save_incident(*case.scope(system.member), **values)
            submit_incident(
                *case.scope(case.proposer),
                action="update",
                idempotency_key=uuid4(),
                incident_id=incident.pk,
                expected_revision=1,
                values={**values, "available_dc_mw": 0},
            )

    def listed() -> None:
        response = client.get(url)
        assert response.status_code == 200, response.content
        rows = response.json()["results"]
        assert len(rows) == outages
        assert all(row["pending_request_id"] for row in rows)

    assert_scale_invariant(seed, listed, small=1, large=10)


def test_markers_never_cross_to_another_plant_of_the_same_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    sibling = sibling_plant(case.system)
    beside = replace(case.system, region=sibling.region)
    here = _propose(case.proposer, case.system, case.plant, _slot(3))
    there = _propose(case.proposer, beside, sibling, _slot(2))
    _propose_outage_update(case, case.proposer, _outage(case))
    sibling_outage = save_incident(
        beside.member,
        beside.tenant.pk,
        beside.region.pk,
        sibling.pk,
        **{**incident_values(), "available_dc_mw": 1.0},
    )
    client = _signed_in(case.proposer)

    listed = client.get(
        f"/api/tenants/{beside.tenant.pk}/regions/{beside.region.pk}/plants/{sibling.pk}/outages/"
    )

    assert _marked(case.proposer, case.system, case.plant) == {_slot(3)[0]: here.pk}
    assert _marked(case.proposer, beside, sibling) == {_slot(2)[0]: there.pk}
    assert listed.status_code == 200, listed.content
    assert [(row["id"], row["pending_request_id"]) for row in listed.json()["results"]] == [
        (sibling_outage.pk, None)
    ]


def test_a_multi_interval_proposal_refusal_names_the_refused_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    client = Client()
    client.force_login(case.proposer)
    system = case.system
    first, second = _slot(3), _slot(2)
    response = client.post(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/"
        f"plants/{case.plant.pk}/change-requests/production/",
        {
            "idempotency_key": str(uuid4()),
            "reason": "Verified on site",
            "configuration_id": case.plant.current_configuration_id,
            "unit": "MWh",
            "changes": [
                {
                    "starts_at": first[0].isoformat(),
                    "ends_at": first[1].isoformat(),
                    "expected_revision_id": None,
                    "action": "set",
                    "energy": 1,
                },
                {
                    "starts_at": second[0].isoformat(),
                    "ends_at": second[1].isoformat(),
                    "expected_revision_id": None,
                    "action": "set",
                    "energy": -1,
                },
            ],
        },
        content_type="application/json",
    )

    assert response.status_code == 400, response.content
    assert set(response.json()["fields"]) == {"changes.1.energy"}
    assert not ChangeRequest.objects.exists()
