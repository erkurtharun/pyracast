from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from pyracast.api.permissions import WorkspaceMemberWritePermission
from pyracast.change_requests.api import ProductionRequestsView
from pyracast.change_requests.models import ChangeRequest, IncidentChange
from pyracast.change_requests.operations import (
    approve_request,
    mark_seen,
    reject_request,
    submit_incident,
    submit_production,
    withdraw_request,
)
from pyracast.change_requests.preparation import production_context
from pyracast.change_requests.queries import request_detail
from pyracast.incidents.operations import save_incident
from pyracast.irradiance import local_day_bounds
from pyracast.plants.measurements import select_measurements
from pyracast.plants.production_changes import (
    ProductionChange,
    change_production,
    prepare_production_changes,
    production_change_warnings,
)
from tests.test_change_request_boundaries import (
    RequestCase,
    incident_values,
    make_request_case,
    production_request,
)
from tests.test_domain import NOW
from tests.test_production_changes import provider
from tests.test_provisional_measurements import DAY, acquisition

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "Incident periods must not overlap."),
        ("tr", "Kesinti d\u00f6nemleri birbiriyle \u00e7ak\u0131\u015fmamal\u0131d\u0131r."),
    ],
)
def test_approval_validation_detail_is_plain_localized_text(
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    expected: str,
) -> None:
    case = make_request_case(monkeypatch)
    request = submit_incident(
        *case.scope(case.proposer),
        action="create",
        idempotency_key=uuid4(),
        values=incident_values(),
    )
    save_incident(*case.scope(case.system.member), **incident_values())
    detail = request_detail(case.system.member, case.system.tenant.pk, request.pk)
    client = Client(headers={"accept-language": language})
    client.force_login(case.system.member)
    response = client.post(
        f"{case.detail_path(request)}approve/",
        {"review_fingerprint": detail["review_fingerprint"]},
        content_type="application/json",
    )
    assert response.status_code == 409
    assert response.json() == {
        "code": "request_conflict",
        "detail": expected,
    }


def production_kwargs(case: RequestCase) -> dict[str, Any]:
    assert case.plant.current_configuration_id is not None
    return {
        "configuration_id": case.plant.current_configuration_id,
        "unit": "MWh",
        "changes": [ProductionChange(NOW - timedelta(hours=1), NOW, None, "set", 1)],
        "reason": "Meter evidence",
        "idempotency_key": uuid4(),
    }


@pytest.mark.parametrize("reason", ["", "   ", "x" * 501])
def test_request_reason_limits_leave_no_pending_request(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    case = make_request_case(monkeypatch)
    kwargs = production_kwargs(case)
    kwargs["reason"] = reason
    with pytest.raises(ValidationError, match="1 to 500"):
        submit_production(*case.scope(case.proposer), **kwargs)
    assert not ChangeRequest.objects.exists()


@pytest.mark.parametrize("count", [0, 101])
def test_member_batch_limit_is_independent_of_canonical_import_limit(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    case = make_request_case(monkeypatch)
    kwargs = production_kwargs(case)
    kwargs["changes"] = kwargs["changes"] * count
    with pytest.raises(ValidationError, match="1 to 100"):
        submit_production(*case.scope(case.proposer), **kwargs)
    assert not ChangeRequest.objects.exists()


@pytest.mark.parametrize(
    ("action", "target", "values", "ends"),
    [
        ("unknown", False, True, False),
        ("create", False, False, False),
        ("create", False, True, True),
        ("update", True, False, False),
        ("resolve", True, True, True),
        ("cancel", True, False, True),
    ],
)
def test_incident_action_rejects_unrelated_or_missing_fields(
    monkeypatch: pytest.MonkeyPatch, action: str, target: bool, values: bool, ends: bool
) -> None:
    case = make_request_case(monkeypatch)
    incident = save_incident(*case.scope(case.system.member), **incident_values())
    with pytest.raises(ValidationError):
        submit_incident(
            *case.scope(case.proposer),
            action=action,
            idempotency_key=uuid4(),
            incident_id=incident.pk if target else None,
            expected_revision=1 if target else None,
            values=incident_values() if values else None,
            ends_at=NOW if ends else None,
        )
    assert not ChangeRequest.objects.exists()
    incident.refresh_from_db()
    assert incident.revision == 1


def test_incident_key_replays_terminal_decision_without_new_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    key = uuid4()
    request = submit_incident(
        *case.scope(case.proposer), action="create", idempotency_key=key, values=incident_values()
    )
    rejected = reject_request(
        case.system.member, case.system.tenant.pk, request.pk, reason="Not confirmed"
    )
    replay = submit_incident(
        *case.scope(case.proposer), action="create", idempotency_key=key, values=incident_values()
    )
    assert replay.pk == rejected.pk
    assert replay.status == "rejected"
    assert IncidentChange.objects.count() == 1


@pytest.mark.parametrize("action", ["withdraw", "seen"])
def test_administrator_cannot_consume_another_members_personal_request_actions(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    case = make_request_case(monkeypatch)
    request = production_request(case)
    if action == "seen":
        reject_request(
            case.system.member, case.system.tenant.pk, request.pk, reason="Not confirmed"
        )
    operation = withdraw_request if action == "withdraw" else mark_seen
    with pytest.raises(ChangeRequest.DoesNotExist):
        operation(case.system.member, case.system.tenant.pk, request.pk)
    request.refresh_from_db()
    assert request.status == ("pending" if action == "withdraw" else "rejected")
    assert request.outcome_seen_at is None


def test_withdrawal_retry_preserves_the_first_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    case = make_request_case(monkeypatch)
    request = production_request(case)
    first = withdraw_request(case.proposer, case.system.tenant.pk, request.pk)
    repeated = withdraw_request(case.proposer, case.system.tenant.pk, request.pk)
    assert repeated.closed_at == first.closed_at
    assert repeated.status == "withdrawn"


def test_workspace_admin_api_decisions_and_filtered_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    request = production_request(case)
    client = Client()
    client.force_login(case.system.member)
    detail = client.get(case.detail_path(request)).json()
    response = client.post(
        f"{case.detail_path(request)}approve/",
        {"review_fingerprint": detail["review_fingerprint"]},
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    kwargs = production_kwargs(case)
    kwargs["changes"] = [
        ProductionChange(
            NOW - timedelta(hours=1),
            NOW,
            request.production_items.get().applied_observation_id,
            "set",
            2,
        )
    ]
    rejected = submit_production(*case.scope(case.proposer), **kwargs)
    response = client.post(
        f"{case.detail_path(rejected)}reject/",
        {"reason": "Not confirmed"},
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    repeated = client.post(
        f"{case.detail_path(rejected)}reject/",
        {"reason": "Different explanation"},
        content_type="application/json",
    )
    assert repeated.status_code == 200
    assert repeated.json()["decision_reason"] == "Not confirmed"
    history = client.get(
        f"/api/tenants/{case.system.tenant.pk}/change-requests/?scope=history&plant_id={case.plant.pk}"
    )
    assert history.status_code == 200
    assert {row["id"] for row in history.json()["results"]} == {request.pk, rejected.pk}
    assert all(not row["can_review"] for row in history.json()["results"])
    assert (
        client.get(
            f"/api/tenants/{case.system.tenant.pk}/change-requests/?plant_id={case.plant.pk + 1}"
        ).json()["results"]
        == []
    )


def test_compiled_turkish_review_conflict_text_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    case = make_request_case(monkeypatch)
    request = production_request(case)
    client = Client(headers={"accept-language": "tr"})
    client.force_login(case.system.member)
    response = client.post(
        f"{case.detail_path(request)}approve/",
        {"review_fingerprint": "stale"},
        content_type="application/json",
    )
    assert response.status_code == 409
    assert response.json() == {
        "code": "review_context_changed",
        "detail": (
            "\u0130nceleme verileri de\u011fi\u015fti. Sayfay\u0131 yenileyip yeniden inceleyin."
        ),
    }


def test_rejected_production_history_preserves_decision_time_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    configuration = case.plant.current_configuration
    assert configuration is not None
    source = configuration, provider(case.system, case.plant)
    acquisition(source)
    start, end = local_day_bounds(DAY, case.system.binding.timezone)
    request = submit_production(
        *case.scope(case.proposer),
        configuration_id=configuration.pk,
        unit="MWh",
        changes=[ProductionChange(start, start + timedelta(hours=1), None, "set", 1)],
        reason="Meter evidence",
        idempotency_key=uuid4(),
    )
    acquisition(source, minute=1, values=["30"] * 24)
    reject_request(case.system.member, case.system.tenant.pk, request.pk, reason="Not confirmed")
    frozen = request_detail(case.system.member, case.system.tenant.pk, request.pk)[
        "production_items"
    ]
    assert frozen[0]["baseline_energy_mwh"] == 26
    assert frozen[0]["current"]["energy_mwh"] == 30
    acquisition(source, minute=2, values=["40"] * 24)
    assert (
        request_detail(case.system.member, case.system.tenant.pk, request.pk)["production_items"]
        == frozen
    )
    assert select_measurements(configuration, start, end, NOW)[0][0].energy_mwh == 40


def test_reverting_the_only_manual_half_hour_restores_a_complete_provider_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    configuration = case.plant.current_configuration
    assert configuration is not None
    acquisition((configuration, provider(case.system, case.plant)))
    start, _ = local_day_bounds(DAY, case.system.binding.timezone)
    saved = change_production(
        *case.scope(case.system.member),
        configuration_id=configuration.pk,
        unit="MWh",
        changes=[ProductionChange(start, start + timedelta(minutes=30), None, "set", 1)],
    )
    with transaction.atomic():
        prepared = prepare_production_changes(
            configuration,
            "MWh",
            [ProductionChange(start, start + timedelta(minutes=30), saved["ids"][0], "revert")],
        )
        assert production_change_warnings(configuration, prepared) == []


def test_completed_incident_proposal_preserves_its_bounded_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    values = {**incident_values(), "ends_at": NOW - timedelta(hours=1)}
    request = submit_incident(
        *case.scope(case.proposer), action="create", idempotency_key=uuid4(), values=values
    )
    detail = request_detail(case.system.member, case.system.tenant.pk, request.pk)
    approved = approve_request(
        case.system.member,
        case.system.tenant.pk,
        request.pk,
        review_fingerprint=detail["review_fingerprint"],
    )
    revision = approved.incident_change.applied_revision
    assert revision is not None
    assert revision.snapshot["ends_at"] == values["ends_at"].isoformat()


def test_request_permission_rejects_an_anonymous_actor() -> None:
    request = Request(APIRequestFactory().post("/"))
    view = ProductionRequestsView()
    view.kwargs = {"tenant_id": 1}
    assert not WorkspaceMemberWritePermission().has_permission(request, view)


def test_production_review_query_count_does_not_grow_with_existing_head_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    configuration_id = case.plant.current_configuration_id
    assert configuration_id is not None
    changes = [
        ProductionChange(
            NOW - timedelta(hours=5 - index), NOW - timedelta(hours=4 - index), None, "set", 1
        )
        for index in range(5)
    ]
    saved = change_production(
        *case.scope(case.system.member),
        configuration_id=configuration_id,
        unit="MWh",
        changes=changes,
    )
    query_counts = []
    for count in (1, 2, 5):
        corrections = [
            ProductionChange(row.starts_at, row.ends_at, head_id, "set", 2)
            for row, head_id in zip(changes[:count], saved["ids"][:count], strict=True)
        ]
        request = submit_production(
            *case.scope(case.proposer),
            configuration_id=configuration_id,
            unit="MWh",
            changes=corrections,
            reason="Verified corrections",
            idempotency_key=uuid4(),
        )
        request = ChangeRequest.objects.select_related("configuration__binding").get(pk=request.pk)
        with CaptureQueriesContext(connection) as captured:
            context = production_context(request)
        assert len(context) == count
        assert all(item["current"]["energy_mwh"] == 1 for item in context)
        assert all(item["proposed"]["energy_mwh"] == 2 for item in context)
        query_counts.append(len(captured))
    assert query_counts == [query_counts[0]] * 3
