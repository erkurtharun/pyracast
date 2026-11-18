from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from typing import Any, cast
from uuid import uuid4

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import connections, transaction
from django.utils import timezone

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.accounts.operations import reassign_workspace_admin, remove_workspace_member
from pyracast.change_requests import operations, preparation, queries
from pyracast.change_requests.models import ChangeRequest
from pyracast.errors import ConflictError
from pyracast.incidents.models import IncidentRevision, PlantIncident
from pyracast.incidents.operations import lock_incident_plant
from pyracast.metering import jobs as metering_jobs
from pyracast.metering.models import EpiasCollectionJob, EpiasSnapshot
from pyracast.plants.models import Plant, PlantEvent, ProductionObservation
from pyracast.plants.production_changes import ProductionChange, change_production
from tests.support.change_request_boundaries import (
    RequestCase,
    approve,
    incident_request,
    make_request_case,
    production_request,
    review_fingerprint,
)
from tests.support.domain import NOW
from tests.support.metering import FixtureClient, connect, day_data

pytestmark = pytest.mark.django_db(transaction=True)


def threaded_call(action: Callable[[], ChangeRequest]) -> ChangeRequest:
    connections.close_all()
    try:
        return action()
    finally:
        connections.close_all()


def blocked_approval(
    case: RequestCase,
    proposal: ChangeRequest,
    monkeypatch: pytest.MonkeyPatch,
    pool: ThreadPoolExecutor,
) -> tuple[Future[ChangeRequest], Event]:
    entered = Event()
    fingerprint = review_fingerprint(case, proposal)

    def require_admin(actor: User, tenant_id: int, **options: Any) -> Membership:
        if actor.pk == case.system.member.pk and options.get("role") == WorkspaceRole.ADMIN:
            entered.set()
        return tenant_membership(actor, tenant_id, **options)

    monkeypatch.setattr(operations, "tenant_membership", require_admin)
    future = pool.submit(
        threaded_call,
        lambda: operations.approve_request(
            case.system.member,
            case.system.tenant.pk,
            proposal.pk,
            review_fingerprint=fingerprint,
        ),
    )
    return future, entered


def test_concurrent_approvals_apply_one_canonical_incident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    proposal = incident_request(case)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=case.system.tenant.pk)
            future, entered = blocked_approval(case, proposal, monkeypatch, pool)
            assert entered.wait(timeout=10)
            first = approve(case, proposal)
        second = future.result(timeout=10)
    assert first.pk == second.pk == proposal.pk
    assert first.status == second.status == "approved"
    assert PlantIncident.objects.count() == 1
    assert IncidentRevision.objects.count() == 1


@pytest.mark.parametrize("winner", ["reject", "withdraw"])
def test_decision_winning_tenant_lock_prevents_waiting_approval(
    monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    case = make_request_case(monkeypatch)
    proposal = incident_request(case)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=case.system.tenant.pk)
            future, entered = blocked_approval(case, proposal, monkeypatch, pool)
            assert entered.wait(timeout=10)
            if winner == "reject":
                operations.reject_request(
                    case.system.member, case.system.tenant.pk, proposal.pk, reason="Wrong equipment"
                )
            else:
                operations.withdraw_request(case.proposer, case.system.tenant.pk, proposal.pk)
        with pytest.raises(ConflictError) as conflict:
            future.result(timeout=10)
    assert conflict.value.code == "request_already_closed"
    proposal.refresh_from_db()
    assert proposal.status == {"reject": "rejected", "withdraw": "withdrawn"}[winner]
    assert not PlantIncident.objects.exists()
    assert not IncidentRevision.objects.exists()


def test_direct_production_write_wins_against_waiting_proposal_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    proposal = production_request(case)
    assert case.plant.current_configuration_id is not None
    item = proposal.production_items.get()
    before_events = PlantEvent.objects.count()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=case.system.tenant.pk)
            future, entered = blocked_approval(case, proposal, monkeypatch, pool)
            assert entered.wait(timeout=10)
            change_production(
                *case.scope(case.system.member),
                configuration_id=case.plant.current_configuration_id,
                unit="MWh",
                changes=[
                    ProductionChange(
                        item.starts_at, item.ends_at, None, "set", 9.0, source="admin-meter"
                    )
                ],
            )
        with pytest.raises(ConflictError) as conflict:
            future.result(timeout=10)
    assert conflict.value.code == "request_conflict"
    proposal.refresh_from_db()
    assert proposal.status == "pending"
    observation = ProductionObservation.objects.get()
    assert (observation.energy_mwh, observation.source) == (9.0, "admin-meter")
    assert PlantEvent.objects.count() == before_events + 1


def test_member_removal_does_not_discard_waiting_explicit_admin_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    proposal = incident_request(case)
    original_membership = Membership.objects.get(user=case.proposer, tenant=case.system.tenant).pk
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=case.system.tenant.pk)
            future, entered = blocked_approval(case, proposal, monkeypatch, pool)
            assert entered.wait(timeout=10)
            remove_workspace_member(case.system.member, case.system.tenant.pk, case.proposer.pk)
        result = future.result(timeout=10)
    assert result.status == "approved"
    assert result.proposer_membership_id == original_membership
    assert not Membership.objects.filter(user=case.proposer, tenant=case.system.tenant).exists()
    assert PlantIncident.objects.get().created_by_id == case.system.member.pk


def test_admin_transfer_revokes_approval_already_waiting_for_tenant_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    proposal = incident_request(case)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=case.system.tenant.pk)
            future, entered = blocked_approval(case, proposal, monkeypatch, pool)
            assert entered.wait(timeout=10)
            reassign_workspace_admin(
                case.system.admin,
                case.system.tenant.pk,
                case.proposer.pk,
                expected_admin_id=case.system.member.pk,
            )
        with pytest.raises(PermissionDenied):
            future.result(timeout=10)
    proposal.refresh_from_db()
    assert proposal.status == "pending"
    assert not PlantIncident.objects.exists()


def test_submission_waiting_for_removed_membership_cannot_create_orphan_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    entered = Event()

    def require_member(actor: User, tenant_id: int, **options: Any) -> Membership:
        if options.get("role") == WorkspaceRole.VIEWER:
            entered.set()
        return tenant_membership(actor, tenant_id, **options)

    monkeypatch.setattr(operations, "tenant_membership", require_member)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=case.system.tenant.pk)
            future = pool.submit(threaded_call, lambda: production_request(case))
            assert entered.wait(timeout=10)
            remove_workspace_member(case.system.member, case.system.tenant.pk, case.proposer.pk)
        with pytest.raises(PermissionDenied):
            future.result(timeout=10)
    assert not ChangeRequest.objects.exists()
    assert not ProductionObservation.objects.exists()


@pytest.mark.parametrize("shared_key", [False, True])
def test_concurrent_submissions_preserve_idempotency_and_the_last_pending_slot(
    monkeypatch: pytest.MonkeyPatch, shared_key: bool
) -> None:
    case = make_request_case(monkeypatch)
    if not shared_key:
        for _ in range(19):
            production_request(case)
    key = uuid4()
    entered = Event()

    def require_member(actor: User, tenant_id: int, **options: Any) -> Membership:
        if options.get("role") == WorkspaceRole.VIEWER:
            entered.set()
        return tenant_membership(actor, tenant_id, **options)

    monkeypatch.setattr(operations, "tenant_membership", require_member)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=case.system.tenant.pk)
            future = pool.submit(threaded_call, lambda: production_request(case, key=key))
            assert entered.wait(timeout=10)
            first = production_request(case, key=key if shared_key else uuid4())
        if shared_key:
            assert future.result(timeout=10).pk == first.pk
        else:
            with pytest.raises(ConflictError) as conflict:
                future.result(timeout=10)
            assert conflict.value.code == "pending_request_limit"
    assert ChangeRequest.objects.count() == (1 if shared_key else 20)
    assert not ProductionObservation.objects.exists()


def test_provider_publication_winning_plant_lock_invalidates_waiting_review_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    monkeypatch.setattr("pyracast.metering.scheduler.get_epias_client", FixtureClient)
    linked = connect(case.plant, case.system.admin)
    job = EpiasCollectionJob.objects.filter(connection=linked).order_by("-target_date").first()
    assert job is not None
    lease = metering_jobs.claim_epias_job(job.pk, NOW)
    assert lease is not None
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(minutes=1))
    data = day_data(FixtureClient().list_plants()[0], job.target_date)
    reading = data.readings[0]
    assert case.plant.current_configuration_id is not None
    proposal = operations.submit_production(
        *case.scope(case.proposer),
        configuration_id=case.plant.current_configuration_id,
        unit="MWh",
        changes=[ProductionChange(reading.period_start, reading.period_end, None, "set", 3)],
        reason="Meter reading",
        idempotency_key=uuid4(),
    )
    fingerprint = review_fingerprint(case, proposal)
    publication_locked, finish_publication, approval_entered = Event(), Event(), Event()
    original_job = metering_jobs._locked_job

    def locked_job(job_id: int) -> EpiasCollectionJob | None:
        row = original_job(job_id)
        publication_locked.set()
        assert finish_publication.wait(timeout=10)
        return row

    def locked_plant(tenant_id: int, region_id: int, plant_id: int) -> Plant:
        approval_entered.set()
        return lock_incident_plant(tenant_id, region_id, plant_id)

    def publish() -> bool:
        connections.close_all()
        try:
            return metering_jobs._publish(lease, data, NOW)
        finally:
            connections.close_all()

    monkeypatch.setattr(metering_jobs, "_locked_job", locked_job)
    monkeypatch.setattr(operations, "lock_incident_plant", locked_plant)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(publish)
        try:
            assert publication_locked.wait(timeout=10)
            approving = pool.submit(
                threaded_call,
                lambda: operations.approve_request(
                    case.system.member,
                    case.system.tenant.pk,
                    proposal.pk,
                    review_fingerprint=fingerprint,
                ),
            )
            assert approval_entered.wait(timeout=10)
        finally:
            finish_publication.set()
        assert publishing.result(timeout=10)
        with pytest.raises(ConflictError) as conflict:
            approving.result(timeout=10)
    assert conflict.value.code == "review_context_changed"
    proposal.refresh_from_db()
    assert proposal.status == "pending"
    assert EpiasSnapshot.objects.filter(job=job).count() == 1
    assert not ProductionObservation.objects.exists()
    assert review_fingerprint(case, proposal) != fingerprint


def test_detail_read_allows_publication_and_binds_fingerprint_to_returned_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    monkeypatch.setattr("pyracast.metering.scheduler.get_epias_client", FixtureClient)
    linked = connect(case.plant, case.system.admin)
    job = EpiasCollectionJob.objects.filter(connection=linked).order_by("-target_date").first()
    assert job is not None
    lease = metering_jobs.claim_epias_job(job.pk, NOW)
    assert lease is not None
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(minutes=1))
    data = day_data(FixtureClient().list_plants()[0], job.target_date)
    reading = data.readings[0]
    assert case.plant.current_configuration_id is not None
    proposal = operations.submit_production(
        *case.scope(case.proposer),
        configuration_id=case.plant.current_configuration_id,
        unit="MWh",
        changes=[ProductionChange(reading.period_start, reading.period_end, None, "set", 3)],
        reason="Meter reading",
        idempotency_key=uuid4(),
    )
    context_ready, finish_read = Event(), Event()
    original_context = preparation.production_context

    def delayed_context(request: ChangeRequest) -> list[dict[str, Any]]:
        items = original_context(request)
        context_ready.set()
        assert finish_read.wait(timeout=10)
        return items

    def read() -> dict[str, Any]:
        connections.close_all()
        try:
            return queries.request_detail(case.system.member, case.system.tenant.pk, proposal.pk)
        finally:
            connections.close_all()

    def publish() -> bool:
        connections.close_all()
        try:
            return metering_jobs._publish(lease, data, NOW)
        finally:
            connections.close_all()

    monkeypatch.setattr(preparation, "production_context", delayed_context)
    monkeypatch.setattr(queries, "production_context", delayed_context)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading_detail = pool.submit(read)
        try:
            assert context_ready.wait(timeout=10)
            publishing = pool.submit(publish)
            assert publishing.result(timeout=10)
        finally:
            finish_read.set()
        detail = reading_detail.result(timeout=10)
    assert detail["production_items"][0]["current"] is None
    assert EpiasSnapshot.objects.filter(job=job).count() == 1
    with pytest.raises(ConflictError) as conflict:
        operations.approve_request(
            case.system.member,
            case.system.tenant.pk,
            proposal.pk,
            review_fingerprint=cast(str, detail["review_fingerprint"]),
        )
    assert conflict.value.code == "review_context_changed"
    proposal.refresh_from_db()
    assert proposal.status == "pending"
    assert not ProductionObservation.objects.exists()
