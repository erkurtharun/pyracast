from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.test import Client

from pyracast.accounts.models import Membership, Tenant
from pyracast.accounts.registration import register_company

pytestmark = pytest.mark.django_db
PASSWORD = "test-only-" + "falcon-cloud-79!"
DETAILS = {
    "company_name": "Sunfield Energy",
    "username": "sunfield.owner",
    "email": "owner@example.com",
    "password": PASSWORD,
}


def test_company_registration_creates_isolated_workspace_and_rotates_session() -> None:
    other = Tenant.objects.create(name="Existing company")
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/session/").json()["csrf_token"]
    assert client.post("/api/session/register/", DETAILS).status_code == 403
    response = client.post(
        "/api/session/register/", DETAILS, content_type="application/json", HTTP_X_CSRFTOKEN=token
    )
    assert response.status_code == 201
    assert "no-store" in response.headers["Cache-Control"]
    user = User.objects.get(username=DETAILS["username"])
    assert user.check_password(PASSWORD)
    assert user.is_active
    assert not user.is_staff
    assert not user.is_superuser
    membership = Membership.objects.get(user=user)
    assert membership.tenant.name == DETAILS["company_name"]
    assert membership.tenant_id != other.pk
    assert client.get("/api/tenants/").json()["results"] == [
        {"id": membership.tenant_id, "name": DETAILS["company_name"]}
    ]
    assert client.get(f"/api/tenants/{other.pk}/regions/").status_code == 403
    assert client.cookies["sessionid"]["httponly"]
    assert client.post("/api/session/logout/", HTTP_X_CSRFTOKEN=token).status_code == 403
    assert (
        client.post(
            "/api/session/register/",
            DETAILS,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=response.json()["csrf_token"],
        ).status_code
        == 403
    )
    assert Membership.objects.count() == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"company_name": " "},
        {"company_name": "x" * 201},
        {"username": "contains spaces"},
        {"username": "x" * 151},
        {"email": "invalid"},
        {"password": "12345678"},
        {"password": "x" * 1025},
        {"is_superuser": True},
        {"tenant_id": 1},
    ],
)
def test_registration_rejects_invalid_input_without_partial_records(
    overrides: dict[str, Any],
) -> None:
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/session/").json()["csrf_token"]
    response = client.post(
        "/api/session/register/",
        DETAILS | overrides,
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 400
    assert not User.objects.exists()
    assert not Tenant.objects.exists()
    assert not Membership.objects.exists()
    assert client.get("/api/session/").json()["user"] is None


def test_registration_preserves_existing_identity() -> None:
    existing = User.objects.create_user(
        username=DETAILS["username"], password=PASSWORD + "original"
    )
    with pytest.raises(ValidationError):
        register_company(**DETAILS)
    existing.refresh_from_db()
    assert existing.check_password(PASSWORD + "original")
    assert not Tenant.objects.exists()


def test_registration_rolls_back_if_membership_cannot_be_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(**kwargs: Any) -> None:
        raise IntegrityError("membership write failed")

    monkeypatch.setattr(Membership.objects, "create", fail)
    with pytest.raises(IntegrityError, match="membership write failed"):
        register_company(**DETAILS)
    assert not User.objects.exists()
    assert not Tenant.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_registration_cannot_create_duplicate_company_or_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = Barrier(2)
    original_save = User.save

    def synchronized_save(self: User, *args: Any, **kwargs: Any) -> None:
        barrier.wait(timeout=10)
        original_save(self, *args, **kwargs)

    monkeypatch.setattr(User, "save", synchronized_save)

    def register() -> str:
        try:
            register_company(**DETAILS)
            return "created"
        except ValidationError:
            return "duplicate"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: register(), range(2)))
    assert sorted(results) == ["created", "duplicate"]
    assert User.objects.count() == Tenant.objects.count() == Membership.objects.count() == 1
