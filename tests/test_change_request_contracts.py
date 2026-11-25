from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext

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
from tests.support.change_request_boundaries import (
    RequestCase,
    incident_values,
    make_request_case,
    production_request,
)
from tests.support.domain import NOW
from tests.support.production_changes import provider
from tests.support.provisional_measurements import DAY, acquisition

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "Another outage already covers part of this time. Change the start or the end."),
        (
            "tr",
            "Bu zaman\u0131n bir k\u0131sm\u0131nda başka bir kesinti kay\u0131tl\u0131. "
            "Başlang\u0131c\u0131 veya bitişi değiştirin.",
        ),
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
        "message": expected,
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
    with pytest.raises(ValidationError, match="at most 500 characters"):
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
        "message": (
            "Bu değişiklik talebi veya santral siz açt\u0131ktan sonra değişti. "
            "Sayfay\u0131 yenileyip yeniden kontrol edin."
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


TRIAGE_FIELDS = (
    "timezone",
    "item_count",
    "period_start",
    "period_end",
    "incident_action",
    "incident_title",
)


def _inbox(case: RequestCase) -> tuple[Client, dict[int, dict[str, Any]]]:
    client = Client()
    client.force_login(case.system.member)
    response = client.get(f"/api/tenants/{case.system.tenant.pk}/change-requests/?scope=pending")
    assert response.status_code == 200
    return client, {row["id"]: row for row in response.json()["results"]}


def _moment(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def test_inbox_summaries_name_the_period_item_count_and_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    configuration_id = case.plant.current_configuration_id
    assert configuration_id is not None
    production = submit_production(
        *case.scope(case.proposer),
        configuration_id=configuration_id,
        unit="MWh",
        changes=[
            ProductionChange(NOW - timedelta(hours=3), NOW - timedelta(hours=2), None, "set", 1),
            ProductionChange(NOW - timedelta(hours=2), NOW - timedelta(hours=1), None, "set", 2),
        ],
        reason="Verified meter readings",
        idempotency_key=uuid4(),
    )
    created = submit_incident(
        *case.scope(case.proposer),
        action="create",
        idempotency_key=uuid4(),
        values={**incident_values(), "starts_at": NOW - timedelta(hours=6)},
    )
    ongoing = save_incident(*case.scope(case.system.member), **incident_values())
    updated = submit_incident(
        *case.scope(case.proposer),
        action="update",
        idempotency_key=uuid4(),
        values={**incident_values(), "title": "Inverter replaced"},
        incident_id=ongoing.pk,
        expected_revision=ongoing.revision,
    )
    resolved = submit_incident(
        *case.scope(case.proposer),
        action="resolve",
        idempotency_key=uuid4(),
        ends_at=NOW - timedelta(minutes=30),
        incident_id=ongoing.pk,
        expected_revision=ongoing.revision,
    )
    cancelled = submit_incident(
        *case.scope(case.proposer),
        action="cancel",
        idempotency_key=uuid4(),
        incident_id=ongoing.pk,
        expected_revision=ongoing.revision,
    )
    timezone_name = case.system.region.timezone
    expected = {
        production.pk: (2, NOW - timedelta(hours=3), NOW - timedelta(hours=1), None, None),
        created.pk: (1, NOW - timedelta(hours=6), None, "create", "Inverter maintenance"),
        updated.pk: (1, ongoing.starts_at, None, "update", "Inverter replaced"),
        resolved.pk: (
            1,
            ongoing.starts_at,
            NOW - timedelta(minutes=30),
            "resolve",
            "Inverter maintenance",
        ),
        cancelled.pk: (1, ongoing.starts_at, None, "cancel", "Inverter maintenance"),
    }
    client, rows = _inbox(case)
    assert rows.keys() == expected.keys()
    for request_id, (count, start, end, action, title) in expected.items():
        summary = rows[request_id]
        assert summary["timezone"] == timezone_name
        assert summary["item_count"] == count
        assert _moment(summary["period_start"]) == start
        assert _moment(summary["period_end"]) == end
        assert summary["incident_action"] == action
        assert summary["incident_title"] == title
        detail = client.get(f"/api/tenants/{case.system.tenant.pk}/change-requests/{request_id}/")
        assert {field: detail.json()[field] for field in TRIAGE_FIELDS} == {
            field: summary[field] for field in TRIAGE_FIELDS
        }


def test_resolve_and_cancel_summaries_describe_the_revision_they_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    ongoing = save_incident(*case.scope(case.system.member), **incident_values())
    resolved = submit_incident(
        *case.scope(case.proposer),
        action="resolve",
        idempotency_key=uuid4(),
        ends_at=NOW - timedelta(minutes=30),
        incident_id=ongoing.pk,
        expected_revision=ongoing.revision,
    )
    cancelled = submit_incident(
        *case.scope(case.proposer),
        action="cancel",
        idempotency_key=uuid4(),
        incident_id=ongoing.pk,
        expected_revision=ongoing.revision,
    )
    save_incident(
        *case.scope(case.system.member),
        incident_id=ongoing.pk,
        expected_revision=ongoing.revision,
        **{**incident_values(), "title": "Grid outage", "starts_at": NOW - timedelta(minutes=20)},
    )
    client, rows = _inbox(case)
    for request, end, action in (
        (resolved, NOW - timedelta(minutes=30), "resolve"),
        (cancelled, None, "cancel"),
    ):
        summary = rows[request.pk]
        assert summary["incident_action"] == action
        assert summary["incident_title"] == "Inverter maintenance"
        assert _moment(summary["period_start"]) == ongoing.starts_at
        assert _moment(summary["period_end"]) == end
        detail = client.get(case.detail_path(request)).json()
        assert {field: detail[field] for field in TRIAGE_FIELDS} == {
            field: summary[field] for field in TRIAGE_FIELDS
        }


def test_inbox_query_count_does_not_grow_with_items(monkeypatch: pytest.MonkeyPatch) -> None:
    case = make_request_case(monkeypatch)
    configuration_id = case.plant.current_configuration_id
    assert configuration_id is not None
    client = Client()
    client.force_login(case.system.member)
    path = f"/api/tenants/{case.system.tenant.pk}/change-requests/?scope=pending"
    assert client.get(path).status_code == 200
    query_counts = []
    for round_number in range(3):
        end = NOW - timedelta(hours=4 * round_number)
        submit_production(
            *case.scope(case.proposer),
            configuration_id=configuration_id,
            unit="MWh",
            changes=[
                ProductionChange(
                    end - timedelta(hours=2), end - timedelta(hours=1), None, "set", 1
                ),
                ProductionChange(end - timedelta(hours=1), end, None, "set", 1),
            ],
            reason="Verified meter readings",
            idempotency_key=uuid4(),
        )
        incident = save_incident(
            *case.scope(case.system.member),
            **{
                **incident_values(),
                "starts_at": end - timedelta(hours=3),
                "ends_at": end - timedelta(hours=2),
            },
        )
        submit_incident(
            *case.scope(case.proposer),
            action="cancel",
            idempotency_key=uuid4(),
            incident_id=incident.pk,
            expected_revision=incident.revision,
        )
        with CaptureQueriesContext(connection) as captured:
            response = client.get(path)
        assert response.status_code == 200
        assert len(response.json()["results"]) == 2 * (round_number + 1)
        query_counts.append(len(captured))
    assert query_counts == [query_counts[0]] * 3
