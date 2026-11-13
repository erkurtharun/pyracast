from collections.abc import Iterator
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, override_settings

from tests.test_api import verified_admin

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
PASSWORD = "test-only-" + "x" * 40
AUTH = "/api/auth/browser/v1/"


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


def member(*, email: str = "member@example.com", active: bool = True) -> User:
    user = User.objects.create_user(
        username=email, email=email, password=PASSWORD, is_active=active
    )
    EmailAddress.objects.create(user=user, email=email, primary=True, verified=True)
    return user


def submit(client: Client, path: str, data: dict[str, Any]) -> Any:
    token = client.get("/api/session/").json()["csrf_token"]
    return client.post(AUTH + path, data, content_type="application/json", HTTP_X_CSRFTOKEN=token)


def test_session_login_logout_and_csrf_rotation() -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    initial_response = client.get("/api/session/")
    initial = initial_response.json()
    assert initial["user"] is None
    assert "no-store" in initial_response.headers["Cache-Control"]
    assert "private" in initial_response.headers["Cache-Control"]
    credentials = {"email": user.email, "password": PASSWORD}
    assert (
        client.post(AUTH + "auth/login", credentials, content_type="application/json").status_code
        == 403
    )
    assert submit(client, "auth/login", credentials).status_code == 200
    assert client.cookies[settings.SESSION_COOKIE_NAME]["httponly"]
    logged_in = client.get("/api/session/").json()
    assert logged_in["user"] == {
        "id": user.pk,
        "username": user.username,
        "email": user.email,
        "can_admin": False,
    }
    assert (
        client.delete(AUTH + "auth/session", HTTP_X_CSRFTOKEN=initial["csrf_token"]).status_code
        == 403
    )
    replay = Client()
    replay.cookies[settings.SESSION_COOKIE_NAME] = client.cookies[
        settings.SESSION_COOKIE_NAME
    ].value
    response = client.delete(AUTH + "auth/session", HTTP_X_CSRFTOKEN=logged_in["csrf_token"])
    assert response.status_code == 401
    assert client.get("/api/session/").json()["user"] is None
    assert replay.get("/api/session/").json()["user"] is None


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("member@example.com", "wrong"),
        ("missing@example.com", PASSWORD),
    ],
)
def test_invalid_credentials_are_generic(email: str, password: str) -> None:
    member()
    client = Client(enforce_csrf_checks=True)
    response = submit(client, "auth/login", {"email": email, "password": password})
    assert response.status_code == 400
    assert response.json()["errors"] == [
        {
            "message": "The email address and/or password you specified are not correct.",
            "code": "email_password_mismatch",
            "param": "password",
        }
    ]
    assert client.get("/api/session/").json()["user"] is None


def test_inactive_account_cannot_create_a_session() -> None:
    user = member(active=False)
    client = Client(enforce_csrf_checks=True)
    response = submit(client, "auth/login", {"email": user.email, "password": PASSWORD})
    assert response.status_code == 401
    assert not response.json()["meta"]["is_authenticated"]
    assert client.get("/api/session/").json()["user"] is None


def test_password_change_requires_current_password_and_invalidates_all_sessions() -> None:
    user = member()
    first, second = Client(enforce_csrf_checks=True), Client(enforce_csrf_checks=True)
    for client in (first, second):
        assert (
            submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code
            == 200
        )
    assert (
        submit(
            first,
            "account/password/change",
            {"current_password": "wrong", "new_password": PASSWORD + "new"},
        ).status_code
        == 400
    )
    assert (
        submit(
            first,
            "account/password/change",
            {"current_password": PASSWORD, "new_password": "short"},
        ).status_code
        == 400
    )
    response = submit(
        first,
        "account/password/change",
        {"current_password": PASSWORD, "new_password": PASSWORD + "new"},
    )
    assert response.status_code == 401
    assert first.get("/api/session/").json()["user"] is None
    assert second.get("/api/session/").json()["user"] is None
    assert (
        submit(first, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 400
    )
    assert (
        submit(first, "auth/login", {"email": user.email, "password": PASSWORD + "new"}).status_code
        == 200
    )


def test_session_schema_exposes_only_projection() -> None:
    client = Client()
    client.force_login(member())
    response = client.get("/api/schema/?format=json")
    assert response.status_code == 200
    session = response.json()["paths"]["/api/session/"]
    assert "get" in session
    assert "patch" not in session


def test_session_administration_access_tracks_live_role_and_mfa() -> None:
    client, user = verified_admin()
    assert client.get("/api/session/").json()["user"]["can_admin"] is True
    user.is_staff = False
    user.save(update_fields=["is_staff"])
    assert client.get("/api/session/").json()["user"]["can_admin"] is False
    user.is_staff = True
    user.is_superuser = False
    user.save(update_fields=["is_staff", "is_superuser"])
    assert client.get("/api/session/").json()["user"]["can_admin"] is False
    user.is_superuser = True
    user.save(update_fields=["is_superuser"])
    Authenticator.objects.filter(user=user).delete()
    assert client.get("/api/session/").json()["user"]["can_admin"] is False
