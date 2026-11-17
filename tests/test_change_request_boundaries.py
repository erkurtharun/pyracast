from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.test import Client

from pyracast.accounts.models import Membership, Tenant
from pyracast.accounts.operations import reassign_workspace_admin, remove_workspace_member
from pyracast.change_requests.models import ChangeRequest, IncidentChange
from pyracast.change_requests.operations import (
    approve_request,
    mark_seen,
    reject_request,
    submit_incident,
    withdraw_request,
)
from pyracast.change_requests.queries import inbox, request_detail
from pyracast.errors import ConflictError
from pyracast.incidents.models import IncidentRevision, PlantIncident
from pyracast.incidents.operations import save_incident, select_incidents
from pyracast.plants.measurements import select_measurements
from pyracast.plants.models import CalibrationRun, PlantEvent, ProductionObservation
from tests.support.change_request_boundaries import (
    approve,
    incident_request,
    incident_values,
    make_request_case,
    production_request,
    review_fingerprint,
)
from tests.support.domain import NOW

pytestmark = pytest.mark.django_db(transaction=True)


def tenant_summary(client: Client, tenant_id: int) -> dict[str, Any]:
    response = client.get("/api/tenants/")
    assert response.status_code == 200, response.content
    return next(row for row in response.json()["results"] if row["id"] == tenant_id)


def test_pending_rejected_and_withdrawn_changes_do_not_enter_calculation_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    configuration = case.plant.current_configuration
    assert configuration is not None
    start, end = NOW - timedelta(hours=3), NOW
    before_measurements = select_measurements(configuration, start, end, NOW)
    before_incidents = select_incidents(case.plant.pk, start, end, NOW)
    before_events = PlantEvent.objects.count()
    production = production_request(case)
    incident = incident_request(case)
    for terminal in (False, True):
        if terminal:
            reject_request(
                case.system.member, case.system.tenant.pk, production.pk, reason="Meter error"
            )
            withdraw_request(case.proposer, case.system.tenant.pk, incident.pk)
        assert select_measurements(configuration, start, end, NOW) == before_measurements
        assert select_incidents(case.plant.pk, start, end, NOW) == before_incidents
        assert not ProductionObservation.objects.exists()
        assert not PlantIncident.objects.exists()
        assert not IncidentRevision.objects.exists()
        assert not CalibrationRun.objects.exists()
        assert PlantEvent.objects.count() == before_events


def test_approval_preserves_normalized_energy_and_separates_proposer_from_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    proposal = production_request(case)
    applied = approve(case, proposal)
    observation = ProductionObservation.objects.get()
    assert applied.status == "approved"
    assert observation.energy_mwh == 1234.567 / 1000
    assert observation.recorded_by_id == case.system.member.pk
    assert applied.proposed_by_id == case.proposer.pk
    assert applied.reviewed_by_id == case.system.member.pk
    assert PlantEvent.objects.filter(action="production_changed").count() == 1


@pytest.mark.parametrize("kind", ["production", "incidents"])
def test_member_submission_and_withdrawal_are_narrow_api_write_exceptions(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    case = make_request_case(monkeypatch)
    client = Client()
    client.force_login(case.proposer)
    payload: dict[str, object] = {
        "idempotency_key": str(uuid4()),
        "reason": "Verified on site",
    }
    if kind == "production":
        payload.update(
            configuration_id=case.plant.current_configuration_id,
            unit="kWh",
            changes=[
                {
                    "starts_at": NOW - timedelta(hours=1),
                    "ends_at": NOW,
                    "expected_revision_id": None,
                    "action": "set",
                    "energy": 1234.567,
                }
            ],
        )
    else:
        payload.update(action="create", values=incident_values())
    path = (
        f"/api/tenants/{case.system.tenant.pk}/regions/{case.system.region.pk}/"
        f"plants/{case.plant.pk}/change-requests/{kind}/"
    )
    response = client.post(path, payload, content_type="application/json")
    assert response.status_code == 201, response.content
    proposal = ChangeRequest.objects.get()
    response = client.post(
        case.detail_path(proposal) + "withdraw/", content_type="application/json"
    )
    assert response.status_code == 200, response.content
    proposal.refresh_from_db()
    assert proposal.status == "withdrawn"
    assert not ProductionObservation.objects.exists()
    assert not PlantIncident.objects.exists()


def test_outcome_read_receipt_is_explicit_idempotent_and_separate_from_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    proposal = production_request(case)
    approve(case, proposal)
    client = Client()
    client.force_login(case.proposer)
    assert client.get(case.detail_path(proposal)).status_code == 200
    proposal.refresh_from_db()
    assert proposal.outcome_seen_at is None
    for _ in range(2):
        response = client.post(
            case.detail_path(proposal) + "seen/", content_type="application/json"
        )
        assert response.status_code == 200, response.content
    proposal.refresh_from_db()
    assert proposal.outcome_seen_at == NOW
    assert not inbox(case.system.member, case.system.tenant.pk).filter(pk=proposal.pk).exists()


@pytest.mark.parametrize("decision", ["reject", "withdraw"])
def test_approved_request_cannot_be_closed_with_an_opposite_decision(
    monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    case = make_request_case(monkeypatch)
    proposal = production_request(case)
    approve(case, proposal)
    actions: dict[str, Callable[[], object]] = {
        "reject": lambda: reject_request(
            case.system.member, case.system.tenant.pk, proposal.pk, reason="Too late"
        ),
        "withdraw": lambda: withdraw_request(case.proposer, case.system.tenant.pk, proposal.pk),
    }
    with pytest.raises(ConflictError) as conflict:
        actions[decision]()
    assert conflict.value.code == "request_already_closed"
    proposal.refresh_from_db()
    assert proposal.status == "approved"
    assert ProductionObservation.objects.count() == 1


@pytest.mark.parametrize("terminal", ["approved", "rejected", "withdrawn"])
def test_submission_key_replays_terminal_result_and_rejects_changed_payload(
    monkeypatch: pytest.MonkeyPatch, terminal: str
) -> None:
    case = make_request_case(monkeypatch)
    key = uuid4()
    proposal = production_request(case, key=key)
    if terminal == "approved":
        approve(case, proposal)
    elif terminal == "rejected":
        reject_request(case.system.member, case.system.tenant.pk, proposal.pk, reason="Wrong meter")
    else:
        withdraw_request(case.proposer, case.system.tenant.pk, proposal.pk)
    replay = production_request(case, key=key)
    assert (replay.pk, replay.status) == (proposal.pk, terminal)
    with pytest.raises(ConflictError, match="new request key"):
        production_request(case, key=key, energy=1000)
    assert ChangeRequest.objects.count() == 1
    assert ProductionObservation.objects.count() == int(terminal == "approved")


def test_removed_and_rejoined_proposer_cannot_recover_old_request_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    proposal = incident_request(case)
    remove_workspace_member(case.system.member, case.system.tenant.pk, case.proposer.pk)
    client = Client()
    client.force_login(case.proposer)
    assert client.get(case.detail_path(proposal)).status_code in {403, 404}
    rejoined = Membership.objects.create(user=case.proposer, tenant=case.system.tenant)
    assert rejoined.pk != proposal.proposer_membership_id
    assert client.get(case.detail_path(proposal)).status_code == 404
    assert (
        client.post(
            case.detail_path(proposal) + "withdraw/", content_type="application/json"
        ).status_code
        == 404
    )
    assert not inbox(case.proposer, case.system.tenant.pk, scope="mine").exists()
    assert inbox(case.system.member, case.system.tenant.pk).filter(pk=proposal.pk).exists()
    approve(case, proposal)
    assert PlantIncident.objects.get().created_by_id == case.system.member.pk


def test_admin_transfer_revokes_old_role_and_allows_new_admin_to_review_own_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    proposal = production_request(case)
    fingerprint = review_fingerprint(case, proposal)
    reassign_workspace_admin(
        case.system.admin,
        case.system.tenant.pk,
        case.proposer.pk,
        expected_admin_id=case.system.member.pk,
    )
    with pytest.raises(PermissionDenied):
        approve_request(
            case.system.member,
            case.system.tenant.pk,
            proposal.pk,
            review_fingerprint=fingerprint,
        )
    assert inbox(case.proposer, case.system.tenant.pk).filter(pk=proposal.pk).exists()
    fingerprint = cast(
        str, request_detail(case.proposer, case.system.tenant.pk, proposal.pk)["review_fingerprint"]
    )
    result = approve_request(
        case.proposer, case.system.tenant.pk, proposal.pk, review_fingerprint=fingerprint
    )
    assert result.proposed_by_id == result.reviewed_by_id == case.proposer.pk
    assert ProductionObservation.objects.get().recorded_by_id == case.proposer.pk


@pytest.mark.parametrize("operation", ["submit", "approve", "reject", "withdraw", "seen"])
def test_database_deactivation_revokes_authority_of_previously_loaded_user(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    case = make_request_case(monkeypatch)
    proposal = production_request(case)
    actor = case.system.member if operation in {"approve", "reject"} else case.proposer
    fingerprint = review_fingerprint(case, proposal)
    User.objects.filter(pk=actor.pk).update(is_active=False)
    assert actor.is_active
    actions: dict[str, Callable[[], object]] = {
        "submit": lambda: production_request(case),
        "approve": lambda: approve_request(
            actor, case.system.tenant.pk, proposal.pk, review_fingerprint=fingerprint
        ),
        "reject": lambda: reject_request(
            actor, case.system.tenant.pk, proposal.pk, reason="Rejected"
        ),
        "withdraw": lambda: withdraw_request(actor, case.system.tenant.pk, proposal.pk),
        "seen": lambda: mark_seen(actor, case.system.tenant.pk, proposal.pk),
    }
    with pytest.raises(PermissionDenied):
        actions[operation]()
    proposal.refresh_from_db()
    assert proposal.status == "pending"
    assert not ProductionObservation.objects.exists()


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_member_cannot_decide_requests_through_api_or_application(
    monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    case = make_request_case(monkeypatch)
    proposal = production_request(case)
    client = Client()
    client.force_login(case.proposer)
    payload = {"review_fingerprint": review_fingerprint(case, proposal), "reason": "Reviewed"}
    response = client.post(
        case.detail_path(proposal) + f"{decision}/", payload, content_type="application/json"
    )
    assert response.status_code == 403, response.content
    actions: dict[str, Callable[[], object]] = {
        "approve": lambda: approve_request(
            case.proposer,
            case.system.tenant.pk,
            proposal.pk,
            review_fingerprint=payload["review_fingerprint"],
        ),
        "reject": lambda: reject_request(
            case.proposer, case.system.tenant.pk, proposal.pk, reason="Reviewed"
        ),
    }
    with pytest.raises(PermissionDenied):
        actions[decision]()
    proposal.refresh_from_db()
    assert proposal.status == "pending"


@pytest.mark.parametrize("identity", ["workspace_admin", "platform_admin", "outsider"])
def test_submission_requires_current_ordinary_membership(
    monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    case = make_request_case(monkeypatch)
    actor = (
        case.system.member
        if identity == "workspace_admin"
        else case.system.admin
        if identity == "platform_admin"
        else User.objects.create_user(username="outsider")
    )
    with pytest.raises(PermissionDenied):
        submit_incident(
            *case.scope(actor), action="create", idempotency_key=uuid4(), values=incident_values()
        )
    assert not ChangeRequest.objects.exists()
    assert not PlantIncident.objects.exists()


@pytest.mark.parametrize("suffix", ["", "withdraw/", "seen/"])
def test_another_workspace_member_cannot_access_the_proposers_request(
    monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    case = make_request_case(monkeypatch)
    proposal = production_request(case)
    colleague = User.objects.create_user(username="colleague")
    Membership.objects.create(user=colleague, tenant=case.system.tenant)
    client = Client()
    client.force_login(colleague)
    path = case.detail_path(proposal) + suffix
    response = (
        client.get(path) if not suffix else client.post(path, content_type="application/json")
    )
    assert response.status_code == 404, response.content
    assert not inbox(colleague, case.system.tenant.pk, scope="mine").exists()
    proposal.refresh_from_db()
    assert proposal.status == "pending"


@pytest.mark.parametrize("action", ["update", "resolve", "cancel"])
@pytest.mark.parametrize("missing", ["incident_id", "expected_revision"])
def test_incident_proposal_rejects_partial_target_identity_through_api(
    monkeypatch: pytest.MonkeyPatch, action: str, missing: str
) -> None:
    case = make_request_case(monkeypatch)
    incident = save_incident(*case.scope(case.system.member), **incident_values())
    payload: dict[str, object] = {
        "action": action,
        "idempotency_key": str(uuid4()),
        "incident_id": incident.pk,
        "expected_revision": incident.revision,
    }
    payload.pop(missing)
    if action == "update":
        payload["values"] = incident_values()
    elif action == "resolve":
        payload["ends_at"] = NOW
    client = Client()
    client.force_login(case.proposer)
    path = (
        f"/api/tenants/{case.system.tenant.pk}/regions/{case.system.region.pk}/"
        f"plants/{case.plant.pk}/change-requests/incidents/"
    )
    response = client.post(path, payload, content_type="application/json")
    assert response.status_code == 400, response.content
    assert not ChangeRequest.objects.exists()
    incident.refresh_from_db()
    assert incident.revision == 1
    assert incident.cancelled_at is None
    assert incident.ends_at is None


@pytest.mark.parametrize("missing", ["incident_id", "expected_revision"])
def test_database_rejects_half_an_existing_incident_target(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    case = make_request_case(monkeypatch)
    incident = save_incident(*case.scope(case.system.member), **incident_values())
    proposal = submit_incident(
        *case.scope(case.proposer),
        action="update",
        incident_id=incident.pk,
        expected_revision=incident.revision,
        values=incident_values(),
        idempotency_key=uuid4(),
    )
    with (
        transaction.atomic(),
        pytest.raises(IntegrityError, match="proposal_incident_target_valid"),
    ):
        IncidentChange.objects.filter(request=proposal).update(**{missing: None})


def test_tenant_poll_counters_follow_live_roles_and_personal_read_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    production = production_request(case)
    incident = incident_request(case)
    other = Tenant.objects.create(name="Other workspace")
    Membership.objects.create(user=case.system.member, tenant=other, role=Membership.Role.ADMIN)
    administrator, member = Client(), Client()
    administrator.force_login(case.system.member)
    member.force_login(case.proposer)
    assert tenant_summary(administrator, case.system.tenant.pk)["pending_review_count"] == 2
    assert tenant_summary(administrator, other.pk)["pending_review_count"] == 0
    assert tenant_summary(member, case.system.tenant.pk)["pending_review_count"] is None
    assert tenant_summary(member, case.system.tenant.pk)["unread_outcome_count"] == 0
    assert administrator.get(case.detail_path(incident)).status_code == 200
    assert tenant_summary(administrator, case.system.tenant.pk)["pending_review_count"] == 2
    approve(case, production)
    assert tenant_summary(administrator, case.system.tenant.pk)["pending_review_count"] == 1
    assert tenant_summary(member, case.system.tenant.pk)["unread_outcome_count"] == 1
    assert member.get(case.detail_path(production)).status_code == 200
    assert tenant_summary(member, case.system.tenant.pk)["unread_outcome_count"] == 1
    assert (
        member.post(
            case.detail_path(production) + "seen/", content_type="application/json"
        ).status_code
        == 200
    )
    assert tenant_summary(member, case.system.tenant.pk)["unread_outcome_count"] == 0
    reassign_workspace_admin(
        case.system.admin,
        case.system.tenant.pk,
        case.proposer.pk,
        expected_admin_id=case.system.member.pk,
    )
    assert tenant_summary(administrator, case.system.tenant.pk)["pending_review_count"] is None
    assert tenant_summary(member, case.system.tenant.pk)["pending_review_count"] == 1
    assert tenant_summary(administrator, other.pk)["pending_review_count"] == 0


@pytest.mark.parametrize("suffix", ["", "approve/", "reject/", "withdraw/", "seen/"])
def test_other_tenant_cannot_read_or_mutate_request_by_known_id(
    monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    case = make_request_case(monkeypatch)
    proposal = production_request(case)
    other = Tenant.objects.create(name="Other tenant")
    outsider = User.objects.create_user(username="outsider")
    Membership.objects.create(user=outsider, tenant=other, role=Membership.Role.ADMIN)
    client = Client()
    client.force_login(outsider)
    path = f"/api/tenants/{other.pk}/change-requests/{proposal.pk}/" + suffix
    payload = (
        {"review_fingerprint": review_fingerprint(case, proposal)}
        if suffix == "approve/"
        else {"reason": "Reviewed"}
        if suffix == "reject/"
        else {}
    )
    response = (
        client.get(path)
        if not suffix
        else client.post(path, payload, content_type="application/json")
    )
    assert response.status_code == 404, response.content
    proposal.refresh_from_db()
    assert proposal.status == "pending"
    assert not ProductionObservation.objects.exists()
