import re
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from django.contrib.auth.models import User
from django.test import Client

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import Membership
from pyracast.accounts.operations import remove_workspace_member
from pyracast.change_requests.models import ChangeRequest
from pyracast.change_requests.serializers import (
    ChangeRequestOutput,
    ChangeRequestSummaryOutput,
)
from pyracast.trash import trash_region
from tests.support.change_request_boundaries import (
    RequestCase,
    approve,
    incident_request,
    make_request_case,
    production_request,
)
from tests.support.domain import NOW

pytestmark = pytest.mark.django_db(transaction=True)

SOURCE = Path(__file__).resolve().parents[1] / "src" / "pyracast"
SUMMARY = set(ChangeRequestSummaryOutput().fields)
DETAIL_ONLY = set(ChangeRequestOutput().fields) - SUMMARY


def client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def production_path(case: RequestCase) -> str:
    return (
        f"/api/tenants/{case.system.tenant.pk}/regions/{case.system.region.pk}/"
        f"plants/{case.plant.pk}/change-requests/production/"
    )


def production_payload(
    case: RequestCase, intervals: int = 1, minutes: int = 60, hours_back: int = 0
) -> dict[str, Any]:
    end = NOW - timedelta(hours=hours_back)
    return {
        "idempotency_key": str(uuid4()),
        "reason": "Verified on site",
        "configuration_id": case.plant.current_configuration_id,
        "unit": "kWh",
        "changes": [
            {
                "starts_at": end - timedelta(minutes=minutes * (index + 1)),
                "ends_at": end - timedelta(minutes=minutes * index),
                "expected_revision_id": None,
                "action": "set",
                "energy": 100 + index,
            }
            for index in range(intervals)
        ],
    }


def test_every_change_request_action_answers_with_the_summary_the_client_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    member, administrator = client_for(case.proposer), client_for(case.system.member)

    submitted = member.post(
        production_path(case), production_payload(case), content_type="application/json"
    )
    proposal = ChangeRequest.objects.get()
    fingerprint = administrator.get(case.detail_path(proposal)).json()["review_fingerprint"]
    approved = administrator.post(
        case.detail_path(proposal) + "approve/",
        {"review_fingerprint": fingerprint},
        content_type="application/json",
    )
    seen = member.post(case.detail_path(proposal) + "seen/", content_type="application/json")
    for hours_back in (1, 2):
        member.post(
            production_path(case),
            production_payload(case, hours_back=hours_back),
            content_type="application/json",
        )
    second, third = ChangeRequest.objects.filter(status="pending").order_by("pk")
    withdrawn = member.post(case.detail_path(second) + "withdraw/", content_type="application/json")
    rejected = administrator.post(
        case.detail_path(third) + "reject/",
        {"reason": "Meter photo missing"},
        content_type="application/json",
    )

    answers = [submitted, approved, seen, withdrawn, rejected]
    assert [answer.status_code for answer in answers] == [201, 200, 200, 200, 200]
    assert [set(answer.json()) for answer in answers] == [SUMMARY] * 5
    assert [answer.json()["status"] for answer in answers] == [
        "pending",
        "approved",
        "approved",
        "withdrawn",
        "rejected",
    ]
    assert seen.json()["unread"] is False
    assert rejected.json()["decision_reason"] == "Meter photo missing"
    assert {"conflict", "review_fingerprint", "production_items", "incident"} == DETAIL_ONLY


def test_a_submission_replayed_after_the_location_was_trashed_returns_its_stored_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    member = client_for(case.proposer)
    payload = production_payload(case)
    first = member.post(production_path(case), payload, content_type="application/json")
    trash_region(case.system.member, case.system.tenant.pk, case.system.region.pk)

    replay = member.post(production_path(case), payload, content_type="application/json")

    assert (replay.status_code, replay.json().get("id")) == (201, first.json()["id"])
    assert ChangeRequest.objects.count() == 1


def test_only_a_reviewer_receives_the_review_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    case = make_request_case(monkeypatch)
    proposal = production_request(case)

    own = client_for(case.proposer).get(case.detail_path(proposal)).json()
    reviewed = client_for(case.system.member).get(case.detail_path(proposal)).json()

    assert own["review_fingerprint"] is None
    assert reviewed["review_fingerprint"].startswith("v1:")


def test_a_day_of_intervals_is_one_request_inside_the_pending_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    for energy in range(19):
        production_request(case, energy=energy + 1)
    member = client_for(case.proposer)

    day = member.post(
        production_path(case),
        production_payload(case, intervals=48, minutes=30),
        content_type="application/json",
    )
    over = member.post(
        production_path(case), production_payload(case), content_type="application/json"
    )

    assert day.status_code == 201, day.content
    assert day.json()["item_count"] == 48
    assert (over.status_code, over.json()["code"]) == (409, "pending_request_limit")
    assert ChangeRequest.objects.filter(status="pending").count() == 20


def test_visibility_pending_and_unread_rules_follow_the_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyracast.change_requests import policy

    case = make_request_case(monkeypatch)
    proposal = production_request(case)
    outage = incident_request(case)
    approve(case, proposal)
    administrator = tenant_membership(case.system.member, case.system.tenant.pk)
    proposer = tenant_membership(case.proposer, case.system.tenant.pk)
    colleague_user = User.objects.create_user(username="colleague")
    colleague = Membership.objects.create(user=colleague_user, tenant=case.system.tenant)

    assert set(policy.visible_requests(administrator)) == {proposal, outage}
    assert set(policy.visible_requests(proposer)) == {proposal, outage}
    assert set(policy.visible_requests(colleague)) == set()
    assert policy.is_author(proposal, proposer)
    assert not policy.is_author(proposal, administrator)
    assert policy.workspace_counters(case.system.member, [administrator]) == {
        case.system.tenant.pk: (1, 0)
    }
    assert policy.workspace_counters(case.proposer, [proposer]) == {
        case.system.tenant.pk: (None, 1)
    }
    remove_workspace_member(case.system.member, case.system.tenant.pk, case.proposer.pk)
    rejoined = Membership.objects.create(user=case.proposer, tenant=case.system.tenant)

    assert set(policy.visible_requests(rejoined)) == set()
    assert policy.workspace_counters(case.proposer, [rejoined]) == {
        case.system.tenant.pk: (None, 0)
    }


def test_the_workspace_counters_leave_out_requests_of_a_trashed_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    production_request(case)
    trash_region(case.system.member, case.system.tenant.pk, case.system.region.pk)

    rows = client_for(case.system.member).get("/api/tenants/").json()["results"]

    assert [(row["pending_review_count"], row["unread_outcome_count"]) for row in rows] == [(0, 0)]


def test_the_change_request_rules_are_read_only_through_the_policy_module() -> None:
    rule = re.compile(r"proposer_membership_id|outcome_seen_at|status=ChangeRequestStatus\.PENDING")
    readers = {
        path.relative_to(SOURCE).as_posix()
        for path in SOURCE.rglob("*.py")
        if "migrations" not in path.parts and rule.search(path.read_text(encoding="utf-8"))
    }

    assert readers == {
        "change_requests/models.py",
        "change_requests/operations.py",
        "change_requests/policy.py",
    }
