from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TypedDict

import pytest
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import Client
from django.urls import reverse
from django.utils import translation

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.errors import DomainError
from pyracast.plants.sites import submit_site
from pyracast.regions import operations
from pyracast.regions.models import (
    AuditEvent,
    Region,
    RegionLocationChangeRequest,
)
from pyracast.regions.operations import request_location_change, submit_region
from tests.support.api import verified_admin
from tests.support.domain import System, make_system
from tests.support.errors import raises_code

pytestmark = pytest.mark.django_db(transaction=True)


class Location(TypedDict):
    latitude: float
    longitude: float
    timezone: str


TARGET: Location = {"latitude": 41.5, "longitude": 30.5, "timezone": "UTC"}
SETUP_CHANGED = (
    "The plant setup changed after this page opened. Reload the plant and review it again."
)
SETUP_CHANGED_TR = (
    "Bu sayfa aç\u0131ld\u0131ktan sonra santral kurulumu değişti. "
    "Santrali yeniden yükleyip tekrar inceleyin."
)


def requests_path(system: System, region: Region | None = None) -> str:
    region = region or system.region
    return f"/api/tenants/{system.tenant.pk}/regions/{region.pk}/location-change-requests/"


def client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def submit(
    system: System, region: Region | None = None, **changes: object
) -> tuple[int, dict[str, object]]:
    region = region or system.region
    region.refresh_from_db()
    response = client_for(system.member).post(
        requests_path(system, region),
        {**TARGET, "expected_revision": region.revision, **changes},
        content_type="application/json",
    )
    return response.status_code, response.json()


def second_region(system: System, **location: float) -> Region:
    return submit_region(
        system.member,
        system.tenant.pk,
        name="Second",
        latitude=location.get("latitude", 38.0),
        longitude=location.get("longitude", 27.0),
        timezone="Europe/Istanbul",
    )


def test_an_occupied_target_is_refused_at_submission_through_the_api() -> None:
    system = make_system(approved=True)
    second_region(system, **{"latitude": 41.5000004, "longitude": 30.5})

    status, body = submit(system)

    assert (status, body.get("code")) == (400, "location_already_exists")
    assert not RegionLocationChangeRequest.objects.exists()


def test_a_target_another_plant_already_requested_is_refused_at_submission() -> None:
    system = make_system(approved=True)
    other = second_region(system)
    operations.configure_binding(
        system.admin,
        other.pk,
        provider_account_id=system.account.pk,
        site_id="second",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    assert submit(system, other)[0] == 201

    status, body = submit(system)

    assert (status, body.get("code")) == (400, "location_already_exists")
    assert RegionLocationChangeRequest.objects.get().region_id == other.pk


def test_concurrent_submissions_to_one_target_accept_exactly_one() -> None:
    system = make_system(approved=True)
    other = second_region(system)
    barrier = Barrier(2)

    def request(region: Region) -> str:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            request_location_change(
                system.member,
                region.pk,
                system.tenant.pk,
                expected_revision=region.revision,
                **TARGET,
            )
            return "requested"
        except ValidationError as error:
            return str(error.code)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request, region) for region in (system.region, other)]
        results = sorted(future.result(timeout=15) for future in futures)

    assert results == ["location_already_exists", "requested"]
    assert RegionLocationChangeRequest.objects.count() == 1


def test_a_customer_requests_withdraws_and_sees_the_operator_approval_and_its_reason() -> None:
    system = make_system(approved=True)
    member = client_for(system.member)
    status, first = submit(system)
    assert (status, first["status"]) == (201, "pending")
    withdraw = f"{requests_path(system)}{first['id']}/withdraw/"

    withdrawn = member.post(withdraw, content_type="application/json")
    replay = member.post(withdraw, content_type="application/json")

    assert (withdrawn.status_code, withdrawn.json()["status"]) == (200, "withdrawn")
    assert withdrawn.json()["reviewed_at"] is not None
    assert replay.json() == withdrawn.json()
    status, second = submit(system, reason="Pin moved to the substation")
    assert (status, second["status"]) == (201, "pending")
    operator, _ = verified_admin()
    system.region.refresh_from_db()

    decided = operator.post(
        reverse("admin:workspace_decision", args=(system.region.pk,)),
        {
            "expected_revision": system.region.revision,
            "expected_status": system.region.status,
            "action": "approve_location",
            "location_change_id": second["id"],
            "decision_reason": "Matches the grid connection record",
        },
    )

    assert decided.status_code == 302
    rows = member.get(requests_path(system)).json()["results"]
    assert [(row["status"], row["decision_reason"]) for row in rows] == [
        ("approved", "Matches the grid connection record"),
        ("withdrawn", ""),
    ]
    system.region.refresh_from_db()
    assert (system.region.latitude, system.region.longitude, system.region.status) == (
        41.5,
        30.5,
        "pending",
    )
    assert list(
        AuditEvent.objects.filter(action__startswith="location_change").values_list(
            "action", flat=True
        )
    ) == [
        "location_change_requested",
        "location_change_withdrawn",
        "location_change_requested",
        "location_change_approved",
    ]


def test_a_withdraw_is_refused_for_a_decided_request_a_viewer_and_another_workspace() -> None:
    system = make_system(approved=True)
    change = request_location_change(
        system.member, system.region.pk, system.tenant.pk, expected_revision=1, **TARGET
    )
    operations.reject_location_change(
        system.admin, change.pk, expected_revision=1, reason="Coordinates point at a lake"
    )
    viewer = User.objects.create(username="viewer")
    Membership.objects.create(user=viewer, tenant=system.tenant, role=WorkspaceRole.VIEWER)
    outsider = User.objects.create(username="outsider")
    Membership.objects.create(
        user=outsider, tenant=Tenant.objects.create(name="Other"), role=WorkspaceRole.ADMIN
    )
    path = f"{requests_path(system)}{change.pk}/withdraw/"

    decided = client_for(system.member).post(path, content_type="application/json")
    by_viewer = client_for(viewer).post(path, content_type="application/json")
    by_outsider = client_for(outsider).post(path, content_type="application/json")

    assert (decided.status_code, decided.json()["code"]) == (
        409,
        "location_change_already_reviewed",
    )
    assert by_viewer.status_code == 403
    assert by_outsider.status_code == 403
    change.refresh_from_db()
    assert (change.status, change.decision_reason) == ("rejected", "Coordinates point at a lake")


def test_a_withdrawn_request_cannot_be_approved_and_a_rejected_one_cannot_be_withdrawn() -> None:
    system = make_system(approved=True)
    change = request_location_change(
        system.member, system.region.pk, system.tenant.pk, expected_revision=1, **TARGET
    )
    operations.withdraw_location_change(
        system.member, system.tenant.pk, system.region.pk, change.pk
    )

    with raises_code("location_change_already_reviewed"):
        operations.approve_location_change(system.admin, change.pk, expected_revision=1)
    system.region.refresh_from_db()
    assert (system.region.latitude, system.region.revision) == (40, 1)


def test_an_operator_decision_on_a_page_opened_before_the_last_revision_is_refused() -> None:
    system = make_system(approved=True)
    change = request_location_change(
        system.member, system.region.pk, system.tenant.pk, expected_revision=1, **TARGET
    )

    with raises_code("region_revision_changed"):
        operations.approve_location_change(system.admin, change.pk, expected_revision=2)
    with raises_code("region_revision_changed"):
        operations.reject_location_change(system.admin, change.pk, expected_revision=2)
    change.refresh_from_db()
    assert change.status == "pending"


def test_a_target_taken_after_submission_is_still_refused_at_approval() -> None:
    system = make_system(approved=True)
    change = request_location_change(
        system.member, system.region.pk, system.tenant.pk, expected_revision=1, **TARGET
    )
    Region.objects.filter(pk=second_region(system).pk).update(**TARGET)

    with raises_code("location_already_exists"):
        operations.approve_location_change(system.admin, change.pk, expected_revision=1)
    change.refresh_from_db()
    assert change.status == "pending"


def test_the_administrator_corrects_a_never_approved_plant_without_a_review() -> None:
    system = make_system()
    region = submit_site(
        system.member,
        system.tenant.pk,
        name="Typo",
        latitude=37.0,
        longitude=27.0,
        timezone="Europe/Istanbul",
        dc_capacity_mw=1.0,
    )

    status, body = submit(system, region, reason="Typed the wrong latitude")

    assert (status, body["status"]) == (201, "approved")
    change = RegionLocationChangeRequest.objects.get(region=region)
    assert (change.status, change.reviewed_by_id) == ("approved", system.member.pk)
    region.refresh_from_db()
    assert (region.latitude, region.longitude, region.revision, region.status) == (
        41.5,
        30.5,
        2,
        "pending",
    )
    assert list(AuditEvent.objects.filter(region=region).values_list("action", "actor_id")) == [
        ("location_change_requested", system.member.pk),
        ("location_change_approved", system.member.pk),
    ]


@pytest.mark.parametrize("state", ["connection_configured", "approved_before", "rejected"])
def test_a_plant_that_is_or_was_in_review_still_needs_an_operator_decision(state: str) -> None:
    system = make_system(approved=state == "approved_before")
    if state == "approved_before":
        change = request_location_change(
            system.member, system.region.pk, system.tenant.pk, expected_revision=1, **TARGET
        )
        operations.approve_location_change(system.admin, change.pk, expected_revision=1)
    if state == "rejected":
        Region.objects.filter(pk=system.region.pk).update(status="rejected")

    status, body = submit(system, latitude=42.5, longitude=31.5)

    assert (status, body["status"]) == (201, "pending")


def test_a_concurrent_withdraw_and_approval_settle_the_request_once() -> None:
    system = make_system(approved=True)
    change = request_location_change(
        system.member, system.region.pk, system.tenant.pk, expected_revision=1, **TARGET
    )
    barrier = Barrier(2)

    def settle(decide: Callable[[], object]) -> str:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            decide()
            return "settled"
        except ValidationError as error:
            return str(error.code)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                settle,
                lambda: operations.withdraw_location_change(
                    system.member, system.tenant.pk, system.region.pk, change.pk
                ),
            ),
            pool.submit(
                settle,
                lambda: operations.approve_location_change(
                    system.admin, change.pk, expected_revision=1
                ),
            ),
        ]
        results = sorted(future.result(timeout=15) for future in futures)

    assert results == ["location_change_already_reviewed", "settled"]
    change.refresh_from_db()
    system.region.refresh_from_db()
    moved = change.status == "approved"
    assert change.status in {"approved", "withdrawn"}
    assert system.region.revision == (2 if moved else 1)
    assert (
        AuditEvent.objects.filter(
            action__in=("location_change_approved", "location_change_withdrawn")
        ).count()
        == 1
    )


def test_the_console_names_why_a_location_decision_was_refused() -> None:
    system = make_system(approved=True)
    change = request_location_change(
        system.member, system.region.pk, system.tenant.pk, expected_revision=1, **TARGET
    )
    operator, _ = verified_admin()

    refused = operator.post(
        reverse("admin:workspace_decision", args=(system.region.pk,)),
        {
            "expected_revision": 2,
            "expected_status": "approved",
            "action": "reject_location",
            "location_change_id": change.pk,
        },
    )

    assert refused.status_code == 302
    assert [str(message) for message in get_messages(refused.wsgi_request)] == [SETUP_CHANGED]
    change.refresh_from_db()
    assert change.status == "pending"


def test_a_decision_after_a_binding_change_names_a_setup_change_not_a_location_change() -> None:
    system = make_system()
    change = request_location_change(
        system.member, system.region.pk, system.tenant.pk, expected_revision=1, **TARGET
    )
    operations.configure_binding(
        system.admin,
        system.region.pk,
        provider_account_id=system.account.pk,
        site_id="replacement",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    operator, _ = verified_admin()

    refused = operator.post(
        reverse("admin:workspace_decision", args=(system.region.pk,)),
        {
            "expected_revision": 1,
            "expected_status": "pending",
            "action": "approve_location",
            "location_change_id": change.pk,
        },
    )

    assert [str(message) for message in get_messages(refused.wsgi_request)] == [SETUP_CHANGED]
    with translation.override("tr"):
        with pytest.raises(DomainError) as refusal:
            operations.approve_location_change(
                system.admin, change.pk, expected_revision=1, expected_status="pending"
            )
        shown = [str(message) for message in refusal.value.messages]
    assert (refusal.value.code, shown) == ("region_revision_changed", [SETUP_CHANGED_TR])
    change.refresh_from_db()
    system.region.refresh_from_db()
    assert (change.status, change.base_revision, system.region.revision) == ("pending", 2, 2)
    assert (system.region.latitude, system.region.longitude) == (40, 29)


REFUSAL_ORDER = (
    ("settled", "location_change_already_reviewed"),
    ("status", "conflict"),
    ("revision", "region_revision_changed"),
    ("target", "location_already_exists"),
)


@pytest.mark.parametrize("step", range(len(REFUSAL_ORDER)), ids=[s for s, _ in REFUSAL_ORDER])
def test_a_location_approval_names_the_first_failing_check_in_a_fixed_order(step: int) -> None:
    system = make_system(approved=True)
    change = request_location_change(
        system.member, system.region.pk, system.tenant.pk, expected_revision=1, **TARGET
    )
    failing = {check for check, _ in REFUSAL_ORDER[step:]}
    Region.objects.filter(pk=second_region(system).pk).update(**TARGET)
    if "settled" in failing:
        operations.withdraw_location_change(
            system.member, system.tenant.pk, system.region.pk, change.pk
        )
    if "revision" in failing:
        Region.objects.filter(pk=system.region.pk).update(revision=2)

    with raises_code(REFUSAL_ORDER[step][1]):
        operations.approve_location_change(
            system.admin,
            change.pk,
            expected_revision=1,
            expected_status="pending" if "status" in failing else "approved",
        )


def test_a_decision_note_longer_than_500_characters_is_refused() -> None:
    system = make_system(approved=True)
    change = request_location_change(
        system.member, system.region.pk, system.tenant.pk, expected_revision=1, **TARGET
    )

    with raises_code("invalid_request"):
        operations.reject_location_change(
            system.admin, change.pk, expected_revision=1, reason="x" * 501
        )
    change.refresh_from_db()
    assert change.status == "pending"
