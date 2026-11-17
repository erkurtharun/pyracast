import ast
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from rest_framework.pagination import PageNumberPagination

from pyracast.accounts.models import Membership
from pyracast.accounts.operations import reassign_workspace_admin, workspace_membership
from pyracast.change_requests import operations, queries
from pyracast.change_requests.models import ChangeRequest, IncidentChange, ProductionChangeItem
from pyracast.change_requests.operations import (
    RequestConflictError,
    approve_request,
    submit_incident,
    submit_production,
)
from pyracast.change_requests.queries import request_detail
from pyracast.incidents.models import IncidentRevision, PlantIncident
from pyracast.incidents.operations import save_incident
from pyracast.irradiance import local_day_bounds
from pyracast.plants.measurements import select_measurements, selection_fingerprint
from pyracast.plants.models import ProductionObservation
from pyracast.plants.operations import revise_plant
from pyracast.plants.production import production_history
from pyracast.plants.production_changes import ProductionChange, change_production
from pyracast.trash import RETENTION, purge_expired, restore_plant, trash_plant
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


def approve(case: RequestCase, request: ChangeRequest) -> ChangeRequest:
    fingerprint = request_detail(case.system.member, case.system.tenant.pk, request.pk)[
        "review_fingerprint"
    ]
    return approve_request(
        case.system.member, case.system.tenant.pk, request.pk, review_fingerprint=fingerprint
    )


def provider_request(
    case: RequestCase, *, action: str = "set", expected: int | None = None
) -> ChangeRequest:
    start, _ = local_day_bounds(DAY, case.system.binding.timezone)
    assert case.plant.current_configuration_id is not None
    return submit_production(
        *case.scope(case.proposer),
        configuration_id=case.plant.current_configuration_id,
        unit="MWh",
        changes=[ProductionChange(start, start + timedelta(hours=1), expected, action, 3)],
        reason="Meter evidence",
        idempotency_key=uuid4(),
    )


def test_provider_review_tracks_semantic_values_and_freezes_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    configuration = case.plant.current_configuration
    assert configuration is not None
    source = configuration, provider(case.system, case.plant)
    acquisition(source)
    request = provider_request(case)
    before = request_detail(case.system.member, case.system.tenant.pk, request.pk)
    assert before["production_items"][0]["current"]["energy_mwh"] == 26
    acquisition(source, minute=1)
    identical = request_detail(case.system.member, case.system.tenant.pk, request.pk)
    assert identical["review_fingerprint"] == before["review_fingerprint"]
    acquisition(source, minute=2, values=["30"] * 24)
    with pytest.raises(RequestConflictError, match="review context changed"):
        approve_request(
            case.system.member,
            case.system.tenant.pk,
            request.pk,
            review_fingerprint=before["review_fingerprint"],
        )
    assert not ProductionObservation.objects.filter(plant=case.plant).exists()
    approve(case, request)
    frozen = request_detail(case.system.member, case.system.tenant.pk, request.pk)
    assert frozen["production_items"][0]["current"]["energy_mwh"] == 30
    assert frozen["production_items"][0]["proposed"]["energy_mwh"] == 3
    acquisition(source, minute=3, values=["40"] * 24)
    assert (
        request_detail(case.system.member, case.system.tenant.pk, request.pk)["production_items"]
        == frozen["production_items"]
    )


def test_revert_proposal_restores_approval_time_automatic_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    configuration = case.plant.current_configuration
    assert configuration is not None
    source = configuration, provider(case.system, case.plant)
    acquisition(source)
    first = approve(case, provider_request(case))
    observation = first.production_items.get().applied_observation_id
    revert = provider_request(case, action="revert", expected=observation)
    detail = request_detail(case.proposer, case.system.tenant.pk, revert.pk)
    assert detail["production_items"][0]["energy_mwh"] is None
    assert detail["production_items"][0]["proposed"]["energy_mwh"] == 26
    acquisition(source, minute=1, values=["31"] * 24)
    approve(case, revert)
    start, end = local_day_bounds(DAY, case.system.binding.timezone)
    assert select_measurements(configuration, start, end, NOW)[0][0].energy_mwh == 31
    assert ProductionObservation.objects.get(is_current=True, plant=case.plant).is_reverted


def test_half_hour_review_preserves_native_provider_evidence_and_coverage_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    configuration = case.plant.current_configuration
    assert configuration is not None
    source = configuration, provider(case.system, case.plant)
    acquisition(source)
    start, _ = local_day_bounds(DAY, case.system.binding.timezone)
    request = submit_production(
        *case.scope(case.proposer),
        configuration_id=configuration.pk,
        unit="MWh",
        changes=[ProductionChange(start, start + timedelta(minutes=30), None, "set", 3)],
        reason="Half-hour meter",
        idempotency_key=uuid4(),
    )
    detail = request_detail(case.system.member, case.system.tenant.pk, request.pk)
    item = detail["production_items"][0]
    assert item["automatic"] is None
    assert item["provider_intervals"] == [
        {
            "starts_at": start,
            "ends_at": start + timedelta(hours=1),
            "energy_mwh": 26,
            "quality": "valid",
            "source_kind": "realtime_generation",
        }
    ]
    assert item["warnings"] == [
        {
            "code": "provider_hour_incomplete",
            "starts_at": start,
            "ends_at": start + timedelta(hours=1),
        }
    ]
    acquisition(source, minute=1, values=["27"] * 24)
    with pytest.raises(RequestConflictError, match="review context changed"):
        approve_request(
            case.system.member,
            case.system.tenant.pk,
            request.pk,
            review_fingerprint=detail["review_fingerprint"],
        )
    approve(case, request)
    observation = ProductionObservation.objects.get(plant=case.plant)
    revert = submit_production(
        *case.scope(case.proposer),
        configuration_id=configuration.pk,
        unit="MWh",
        changes=[ProductionChange(start, start + timedelta(minutes=30), observation.pk, "revert")],
        reason="Restore automatic",
        idempotency_key=uuid4(),
    )
    assert (
        request_detail(case.system.member, case.system.tenant.pk, revert.pk)["production_items"][0][
            "warnings"
        ]
        == []
    )
    approve(case, revert)
    restored = select_measurements(configuration, start, start + timedelta(hours=1), NOW)[0]
    assert len(restored) == 1
    assert restored[0].energy_mwh == 27
    assert restored[0].ends_at - restored[0].starts_at == timedelta(hours=1)


@pytest.mark.parametrize("energy", [1234.567, 0.1, 1e-9])
def test_normalized_energy_is_exact_through_approval(
    monkeypatch: pytest.MonkeyPatch, energy: float
) -> None:
    case = make_request_case(monkeypatch)
    request = production_request(case, energy=energy)
    assert request.production_items.get().energy_mwh == energy / 1000
    approved = approve(case, request)
    observation = approved.production_items.get().applied_observation
    assert observation is not None
    assert observation.energy_mwh == energy / 1000


@pytest.mark.parametrize("energy", [float("nan"), float("inf"), -1.0, True])
def test_invalid_production_numbers_do_not_create_requests(
    monkeypatch: pytest.MonkeyPatch, energy: float
) -> None:
    case = make_request_case(monkeypatch)
    with pytest.raises(ValidationError):
        production_request(case, energy=energy)
    assert not ChangeRequest.objects.exists()


def test_pending_changes_do_not_enter_measurements_or_selection_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    configuration = case.plant.current_configuration
    assert configuration is not None
    before = select_measurements(configuration, NOW - timedelta(days=1), NOW, NOW)[0]
    fingerprint = selection_fingerprint(configuration, before, NOW)
    production_request(case)
    submit_incident(
        *case.scope(case.proposer),
        action="create",
        idempotency_key=uuid4(),
        values=incident_values(),
    )
    after = select_measurements(configuration, NOW - timedelta(days=1), NOW, NOW)[0]
    assert before == after
    assert selection_fingerprint(configuration, after, NOW) == fingerprint
    assert not PlantIncident.objects.exists()
    assert not IncidentRevision.objects.exists()


def test_unrelated_production_change_does_not_change_review_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    request = production_request(case)
    before = request_detail(case.system.member, case.system.tenant.pk, request.pk)
    assert case.plant.current_configuration_id is not None
    change_production(
        *case.scope(case.system.member),
        configuration_id=case.plant.current_configuration_id,
        unit="MWh",
        changes=[
            ProductionChange(NOW - timedelta(hours=3), NOW - timedelta(hours=2), None, "set", 2)
        ],
    )
    assert (
        request_detail(case.system.member, case.system.tenant.pk, request.pk)["review_fingerprint"]
        == before["review_fingerprint"]
    )


def test_incident_create_update_resolve_cancel_use_canonical_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    request = submit_incident(
        *case.scope(case.proposer),
        action="create",
        idempotency_key=uuid4(),
        values=incident_values(),
    )
    approve(case, request)
    incident = PlantIncident.objects.get()
    for action, values, ends_at in (
        ("update", {**incident_values(), "available_dc_mw": 0}, None),
        ("resolve", None, NOW - timedelta(hours=1)),
        ("cancel", None, None),
    ):
        previous_revision = incident.revision
        proposed = submit_incident(
            *case.scope(case.proposer),
            action=action,
            idempotency_key=uuid4(),
            incident_id=incident.pk,
            expected_revision=incident.revision,
            values=values,
            ends_at=ends_at,
        )
        assert PlantIncident.objects.get(pk=incident.pk).revision == previous_revision
        approve(case, proposed)
        incident.refresh_from_db()
        assert incident.revision == previous_revision + 1
        proposed.refresh_from_db()
        assert proposed.incident_change.applied_revision is not None
    assert incident.cancelled_at is not None
    assert IncidentRevision.objects.count() == 4


def test_incident_overlap_and_configuration_change_are_derived_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    request = submit_incident(
        *case.scope(case.proposer),
        action="create",
        idempotency_key=uuid4(),
        values=incident_values(),
    )
    save_incident(*case.scope(case.system.member), **incident_values())
    with pytest.raises(RequestConflictError):
        approve(case, request)
    request.refresh_from_db()
    assert request.status == "pending"
    assert (
        request_detail(case.system.member, case.system.tenant.pk, request.pk)["conflict"]
        is not None
    )
    revise_plant(*case.scope(case.system.member), name="Plant", dc_capacity_mw=150)
    with pytest.raises(RequestConflictError, match="configuration changed"):
        approve(case, request)
    detail = request_detail(case.system.member, case.system.tenant.pk, request.pk)
    assert detail["can_review"]
    operations.reject_request(
        case.system.member, case.system.tenant.pk, request.pk, reason="Configuration changed"
    )
    request.refresh_from_db()
    assert request.status == "rejected"


def test_approval_failure_rolls_back_effect_and_frozen_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    request = production_request(case)

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Database unavailable at decision recording")

    monkeypatch.setattr(operations, "_close", unavailable)
    with pytest.raises(RuntimeError, match="decision recording"):
        approve(case, request)
    request.refresh_from_db()
    assert request.status == "pending"
    assert request.reviewed_context == {}
    assert not ProductionObservation.objects.exists()
    assert request.production_items.get().applied_observation_id is None


def test_member_capabilities_preserve_direct_write_restrictions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    day = NOW.date() - timedelta(days=1)
    result = production_history(case.plant, actor=case.proposer, start_date=day, end_date=day)
    assert all(not row["can_edit"] for row in result["intervals"])
    assert all(row["can_propose"] for row in result["intervals"])
    assert all(not row["can_propose_revert"] for row in result["intervals"])

    slot, _ = local_day_bounds(day, case.system.binding.timezone)
    slot += timedelta(hours=10)
    assert case.plant.current_configuration_id is not None
    change_production(
        *case.scope(case.system.member),
        configuration_id=case.plant.current_configuration_id,
        unit="MWh",
        changes=[ProductionChange(slot, slot + timedelta(hours=1), None, "set", 1)],
    )
    result = production_history(case.plant, actor=case.proposer, start_date=day, end_date=day)
    recorded = next(row for row in result["intervals"] if row["starts_at"] == slot)
    assert recorded["can_propose"]
    assert recorded["can_propose_revert"]
    assert not recorded["can_edit"]
    assert not recorded["can_revert"]


def test_trash_restore_and_purge_preserve_then_remove_request_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    production_request(case)
    submit_incident(
        *case.scope(case.proposer),
        action="create",
        idempotency_key=uuid4(),
        values=incident_values(),
    )
    trash_plant(*case.scope(case.system.member))
    with pytest.raises(ChangeRequest.DoesNotExist):
        request_detail(
            case.proposer,
            case.system.tenant.pk,
            ChangeRequest.objects.order_by("pk").get(kind="production").pk,
        )
    restore_plant(*case.scope(case.system.member))
    assert ChangeRequest.objects.count() == 2
    trash_plant(*case.scope(case.system.member))
    assert purge_expired(NOW + RETENTION)["plants"] == 1
    assert not ChangeRequest.objects.exists()
    assert not ProductionChangeItem.objects.exists()
    assert not IncidentChange.objects.exists()


def test_database_rejects_null_existing_incident_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    case = make_request_case(monkeypatch)
    request = submit_incident(
        *case.scope(case.proposer),
        action="create",
        idempotency_key=uuid4(),
        values=incident_values(),
    )
    incident = save_incident(*case.scope(case.system.member), **incident_values())
    with pytest.raises(IntegrityError), transaction.atomic():
        IncidentChange.objects.filter(request=request).update(
            action="update", incident=incident, expected_revision=None
        )


def test_calculation_modules_do_not_import_requests() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "pyracast"
    for relative in (
        "plants/measurements.py",
        "plants/calibration_data.py",
        "plants/history.py",
        "plants/forecast_archive.py",
        "notifications/report_data.py",
    ):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        assert all(
            not (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith("pyracast.change_requests")
            )
            for node in ast.walk(tree)
        )


def test_inbox_pagination_preserves_strict_filter_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    first = production_request(case, energy=1)
    second = production_request(case, energy=2)
    monkeypatch.setattr(PageNumberPagination, "page_size", 1)
    client = Client()
    client.force_login(case.proposer)
    url = f"/api/tenants/{case.system.tenant.pk}/change-requests/?scope=mine"
    response = client.get(f"{url}&page=1")
    assert response.status_code == 200
    assert response.json()["count"] == 2
    assert [row["id"] for row in response.json()["results"]] == [second.pk]
    response = client.get(f"{url}&page=2")
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["results"]] == [first.pk]
    assert client.get(f"{url}&unexpected=1").status_code == 400


def test_detail_rechecks_ownership_when_final_membership_read_sees_admin_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    request = production_request(case)
    client = Client()
    client.force_login(case.system.member)

    def transferred_membership(actor: User, tenant_id: int) -> Membership:
        reassign_workspace_admin(
            case.system.admin, tenant_id, case.proposer.pk, expected_admin_id=case.system.member.pk
        )
        return workspace_membership(actor, tenant_id)

    monkeypatch.setattr(queries, "workspace_membership", transferred_membership)
    response = client.get(f"/api/tenants/{case.system.tenant.pk}/change-requests/{request.pk}/")
    assert response.status_code == 404
    assert response.json() == {"code": "not_found"}


def test_stale_production_proposal_answers_409_and_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    approve(case, production_request(case))
    stored = list(ChangeRequest.objects.values_list("pk", flat=True))
    client = Client()
    client.force_login(case.proposer)
    response = client.post(
        f"/api/tenants/{case.system.tenant.pk}/regions/{case.system.region.pk}/"
        f"plants/{case.plant.pk}/change-requests/production/",
        {
            "idempotency_key": str(uuid4()),
            "reason": "Verified on site",
            "configuration_id": case.plant.current_configuration_id,
            "unit": "kWh",
            "changes": [
                {
                    "starts_at": NOW - timedelta(hours=1),
                    "ends_at": NOW,
                    "expected_revision_id": None,
                    "action": "set",
                    "energy": 1000,
                }
            ],
        },
        content_type="application/json",
    )
    assert response.status_code == 409, response.content
    assert response.json() == {
        "code": "production_revision_conflict",
        "detail": "Production changed. Reload the latest values before saving.",
    }
    assert list(ChangeRequest.objects.values_list("pk", flat=True)) == stored
