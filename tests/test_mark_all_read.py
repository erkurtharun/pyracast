import pytest
from django.contrib.auth.models import User
from django.test import Client

from pyracast.change_requests.models import ChangeRequest
from tests.support.change_request_boundaries import (
    approve,
    incident_request,
    make_request_case,
    production_request,
)

pytestmark = pytest.mark.django_db(transaction=True)


def signed_in(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def test_a_proposer_marks_every_decided_request_read_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    decided = approve(case, production_request(case))
    pending = incident_request(case)
    url = f"/api/tenants/{case.system.tenant.pk}/change-requests/seen/"
    proposer = signed_in(case.proposer)

    first = proposer.post(url, {}, content_type="application/json")
    second = proposer.post(url, {}, content_type="application/json")
    tenants = proposer.get("/api/tenants/").json()["results"]

    assert (first.status_code, first.json()) == (200, {"marked": 1})
    assert second.json() == {"marked": 0}
    assert ChangeRequest.objects.get(pk=decided.pk).outcome_seen_at is not None
    assert ChangeRequest.objects.get(pk=pending.pk).outcome_seen_at is None
    assert tenants[0]["unread_outcome_count"] == 0


def test_marking_all_read_touches_only_the_actor_s_own_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    decided = approve(case, production_request(case))
    url = f"/api/tenants/{case.system.tenant.pk}/change-requests/seen/"
    outsider = User.objects.create_user(username="outsider")

    administrator = signed_in(case.system.member).post(url, {}, content_type="application/json")
    refused = signed_in(outsider).post(url, {}, content_type="application/json")

    assert administrator.json() == {"marked": 0}
    assert (refused.status_code, refused.json()["code"]) == (403, "membership_required")
    assert ChangeRequest.objects.get(pk=decided.pk).outcome_seen_at is None
