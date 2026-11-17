import pytest
from django.contrib.auth.models import User
from django.test import Client

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("is_superuser", [False, True])
def test_tenant_list_includes_only_authenticated_users_memberships(is_superuser: bool) -> None:
    user = User.objects.create_user(username="member", is_superuser=is_superuser)
    other = User.objects.create_user(username="other")
    first = Tenant.objects.create(name="First")
    hidden = Tenant.objects.create(name="Hidden")
    second = Tenant.objects.create(name="Second")
    Membership.objects.create(user=user, tenant=second)
    Membership.objects.create(user=user, tenant=first, role=WorkspaceRole.ADMIN)
    Membership.objects.create(user=other, tenant=hidden)
    client = Client()
    client.force_login(user)

    response = client.get("/api/tenants/")

    assert response.status_code == 200
    assert response.json() == {
        "count": 2,
        "next": None,
        "previous": None,
        "results": [
            {
                "id": first.pk,
                "name": "First",
                "role": "admin",
                "pending_review_count": 0,
                "unread_outcome_count": 0,
            },
            {
                "id": second.pk,
                "name": "Second",
                "role": "viewer",
                "pending_review_count": None,
                "unread_outcome_count": 0,
            },
        ],
    }


def test_tenant_list_is_empty_without_memberships() -> None:
    user = User.objects.create_user(username="new-customer")
    Tenant.objects.create(name="Hidden")
    client = Client()
    client.force_login(user)

    response = client.get("/api/tenants/")

    assert response.status_code == 200
    assert response.json()["count"] == 0
    assert response.json()["results"] == []


def test_anonymous_tenant_listing_is_denied() -> None:
    response = Client().get("/api/tenants/")

    assert response.status_code == 403


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_tenant_list_does_not_allow_writes(method: str) -> None:
    user = User.objects.create_user(username="customer")
    client = Client()
    client.force_login(user)

    response = getattr(client, method)("/api/tenants/", {}, content_type="application/json")

    assert response.status_code == 405
    assert not Tenant.objects.exists()
