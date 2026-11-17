import json
from collections.abc import Iterator
from io import StringIO
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
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
    assert initial["language"] == "en"
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
    assert logged_in["language"] == "en"
    assert logged_in["user"] == {
        "id": user.pk,
        "username": user.username,
        "email": user.email,
        "has_usable_password": True,
        "can_admin": False,
        "mfa_required": False,
        "language_preference": None,
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
    assert response.json()["meta"]["password_changed"] is True
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
    output = StringIO()
    call_command("spectacular", format="openapi-json", stdout=output)
    session = json.loads(output.getvalue())["paths"]["/api/session/"]
    assert "get" in session
    assert "patch" not in session


def test_session_exposes_password_availability_without_credentials() -> None:
    user = member()
    user.set_unusable_password()
    user.save(update_fields=["password"])
    client = Client()
    client.force_login(user)
    data = client.get("/api/session/").json()["user"]
    assert data["has_usable_password"] is False
    assert "password" not in data


def test_expired_session_cannot_report_a_successful_password_change() -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    response = submit(
        client,
        "account/password/change",
        {"new_password": PASSWORD + "new"},
    )
    assert response.status_code == 401
    assert not response.json().get("meta", {}).get("password_changed")
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


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


@pytest.mark.parametrize(
    ("staff", "superuser"), [(False, False), (True, False), (False, True), (True, True)]
)
def test_session_mfa_requirement_tracks_privileged_roles(staff: bool, superuser: bool) -> None:
    user = member()
    user.is_staff, user.is_superuser = staff, superuser
    user.save(update_fields=["is_staff", "is_superuser"])
    client = Client()
    client.force_login(user)
    assert client.get("/api/session/").json()["user"]["mfa_required"] is (staff or superuser)
    response = client.patch(
        "/api/session/", {"mfa_required": False}, content_type="application/json"
    )
    assert response.status_code == 405
