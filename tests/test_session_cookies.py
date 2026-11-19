import time
from collections.abc import Callable
from typing import Any

import pytest
from allauth.account.internal.flows.email_verification_by_code import (
    EMAIL_VERIFICATION_CODE_SESSION_KEY,
)
from allauth.account.models import EmailAddress
from allauth.core.internal import ratelimit
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.test import Client, override_settings

from pyracast import settings as production
from pyracast.accounts import middleware as account_middleware
from tests.support.account_sessions import (
    account,
    browser,
    moment,
    session_key,
    session_user,
    signed_in,
)
from tests.support.auth_lifecycle import AUTH, PASSWORD, mailed_code, submit

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


# tests/settings.py pins PYRACAST_DEBUG=0 before importing pyracast.settings, so this module holds
# the production cookie configuration that the test overlay replaces.
@override_settings(
    SESSION_COOKIE_NAME=production.SESSION_COOKIE_NAME,
    CSRF_COOKIE_NAME=production.CSRF_COOKIE_NAME,
    SESSION_COOKIE_SECURE=production.SESSION_COOKIE_SECURE,
    CSRF_COOKIE_SECURE=production.CSRF_COOKIE_SECURE,
    CSRF_TRUSTED_ORIGINS=["https://testserver"],
)
def test_production_cookie_attributes_satisfy_the_host_prefix() -> None:
    assert production.DEBUG is False
    assert production.SESSION_COOKIE_NAME == "__Host-pyracast-sessionid"
    assert production.CSRF_COOKIE_NAME == "__Host-pyracast-csrftoken"
    user = User.objects.create_user(
        username="member", email="member@example.com", password=PASSWORD
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    client = Client(enforce_csrf_checks=True)

    session = client.get("/api/session/", secure=True)
    token = session.json()["csrf_token"]
    login = client.post(
        "/api/auth/browser/v1/auth/login",
        {"email": user.email, "password": PASSWORD},
        content_type="application/json",
        secure=True,
        headers={"X-CSRFToken": token, "Origin": "https://testserver"},
    )

    assert login.status_code == 200
    cookies = [
        session.cookies[production.CSRF_COOKIE_NAME],
        login.cookies[production.SESSION_COOKIE_NAME],
    ]
    for cookie in cookies:
        header = cookie.OutputString()
        assert cookie["secure"] is True
        assert cookie["path"] == "/"
        assert cookie["domain"] == ""
        assert "; Secure" in header
        assert "Path=/" in header
        assert "Domain" not in header


def session_cookie(response: Any) -> tuple[str, Any, str] | None:
    cookie = response.cookies.get(settings.SESSION_COOKIE_NAME)
    return None if cookie is None else (cookie.value, cookie["max-age"], cookie["expires"])


def arrive(jar: Client, *answers: Any) -> None:
    # A browser applies the session cookie of each answer in the order the answers arrive.
    for answer in answers:
        cookie = answer.cookies.get(settings.SESSION_COOKIE_NAME)
        if cookie is None:
            continue
        if cookie["max-age"] == 0:
            jar.cookies.pop(settings.SESSION_COOKIE_NAME, None)
        else:
            jar.cookies[settings.SESSION_COOKIE_NAME] = cookie.value


def carrying(key: str) -> tuple[Client, str]:
    client = browser()
    client.cookies[settings.SESSION_COOKIE_NAME] = key
    return client, client.get("/api/session/").json()["csrf_token"]


def test_an_activity_refresh_saves_the_session_without_sending_its_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = signed_in(account("owner"))
    later = time.time() + 61
    monkeypatch.setattr(account_middleware, "time", moment(later))

    response = client.get("/api/session/")

    assert response.status_code == 200
    assert session_cookie(response) is None
    assert client.session["last_activity_at"] == later


def test_a_request_that_starts_a_step_saves_the_session_without_sending_its_cookie() -> None:
    client = signed_in(account("owner"), reauthenticated=True)

    response = submit(client, "account/email", {"email": "changed@example.com"})

    assert response.status_code == 200
    assert session_cookie(response) is None
    assert EMAIL_VERIFICATION_CODE_SESSION_KEY in client.session


def test_a_created_or_rotated_session_key_is_sent_as_a_browser_session_cookie() -> None:
    user = account("owner")
    client = browser()

    signing_in = submit(client, "auth/login", {"email": user.email, "password": PASSWORD})
    created = session_key(client)
    rotating = submit(client, "auth/reauthenticate", {"password": PASSWORD})

    assert session_cookie(signing_in) == (created, "", "")
    assert session_cookie(rotating) == (session_key(client), "", "")
    assert session_key(client) != created


def end_by_sign_out(client: Client, monkeypatch: pytest.MonkeyPatch) -> Any:
    token = client.get("/api/session/").json()["csrf_token"]
    return client.delete(AUTH + "auth/session", HTTP_X_CSRFTOKEN=token)


def end_by_password_change(client: Client, monkeypatch: pytest.MonkeyPatch) -> Any:
    change = {"current_password": PASSWORD, "new_password": PASSWORD + "changed"}
    return submit(client, "account/password/change", change)


@pytest.mark.parametrize("end", [end_by_sign_out, end_by_password_change])
def test_a_request_that_ends_its_session_deletes_the_cookie(
    end: Callable[[Client, pytest.MonkeyPatch], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = signed_in(account("owner"))
    key = session_key(client)

    response = end(client, monkeypatch)

    assert session_cookie(response) == ("", 0, "Thu, 01 Jan 1970 00:00:00 GMT")
    assert not Session.objects.filter(session_key=key).exists()


LAPSES = pytest.mark.parametrize(
    "seconds",
    [settings.PYRACAST_SESSION_IDLE_SECONDS, settings.PYRACAST_SESSION_ABSOLUTE_SECONDS],
    ids=["idle", "absolute"],
)


def after_lapse(client: Client, seconds: int, monkeypatch: pytest.MonkeyPatch) -> Any:
    with monkeypatch.context() as later:
        later.setattr(account_middleware, "time", moment(time.time() + seconds))
        return client.get("/api/tenants/")


@LAPSES
def test_a_request_that_finds_its_session_expired_ends_it_and_leaves_the_cookie(
    seconds: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = signed_in(account("owner"))
    key = session_key(client)

    response = after_lapse(client, seconds, monkeypatch)

    assert (response.status_code, response.json()["code"]) == (401, "not_authenticated")
    assert session_cookie(response) is None
    assert not Session.objects.filter(session_key=key).exists()
    assert session_user(client) is None


@LAPSES
def test_a_late_expiry_answer_keeps_the_session_signed_in_after_it(
    seconds: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = account("owner")
    client = signed_in(user)
    old = session_key(client)
    background = browser()
    background.cookies[settings.SESSION_COOKIE_NAME] = old

    expiry = after_lapse(background, seconds, monkeypatch)
    signing_in = submit(client, "auth/login", {"email": user.email, "password": PASSWORD})
    assert (expiry.status_code, expiry.json()["code"]) == (401, "not_authenticated")
    assert signing_in.status_code == 200
    jar = browser()
    jar.cookies[settings.SESSION_COOKIE_NAME] = old

    arrive(jar, signing_in, expiry)

    assert session_key(jar) == session_key(client) != old
    assert session_user(jar) == user.pk
    assert not Session.objects.filter(session_key=old).exists()


def read_session(client: Client, token: str) -> Any:
    return client.get("/api/session/")


def read_workspaces(client: Client, token: str) -> Any:
    return client.get("/api/tenants/")


def sign_out(client: Client, token: str) -> Any:
    return client.delete(AUTH + "auth/session", HTTP_X_CSRFTOKEN=token)


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        (read_session, (200, None)),
        (read_workspaces, (401, "not_authenticated")),
        (sign_out, (401, "not_authenticated")),
    ],
)
def test_a_request_whose_session_no_longer_exists_leaves_the_cookie_alone(
    request_: Callable[[Client, str], Any], expected: tuple[int, str | None]
) -> None:
    user = account("owner")
    client = signed_in(user)
    stale, token = carrying(session_key(client))
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200

    response = request_(stale, token)

    body = response.json()
    assert (response.status_code, body.get("code")) == expected
    assert body.get("user") is None
    assert session_cookie(response) is None
    assert session_user(client) == user.pk


@pytest.mark.parametrize("order", ["processed before", "processed after"])
def test_a_late_answer_around_a_reauthentication_keeps_the_rotated_session(
    order: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = account("owner")
    client = signed_in(user)
    old = session_key(client)
    stale, _ = carrying(old)

    def reauthenticate() -> Any:
        return submit(client, "auth/reauthenticate", {"password": PASSWORD})

    if order == "processed before":
        # The background read records its activity, so it saves the session it carries.
        monkeypatch.setattr(account_middleware, "time", moment(time.time() + 61))
        late = stale.get("/api/tenants/")
        rotation = reauthenticate()
        assert late.status_code == 200
    else:
        rotation = reauthenticate()
        late = stale.get("/api/tenants/")
        assert (late.status_code, late.json()["code"]) == (401, "not_authenticated")
    assert rotation.status_code == 200
    jar = browser()
    jar.cookies[settings.SESSION_COOKIE_NAME] = old

    arrive(jar, rotation, late)

    assert session_key(jar) == session_key(client) != old
    assert session_user(jar) == user.pk
    assert not Session.objects.filter(session_key=old).exists()


@pytest.mark.parametrize("order", ["processed before", "processed after"])
def test_a_late_answer_around_a_sign_in_keeps_the_signed_in_session(
    order: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = account("owner", verified=False)
    client = browser()
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 401
    old = session_key(client)
    stale, token = carrying(old)
    monkeypatch.setattr(ratelimit, "time", moment(time.time() + 61))

    def resend() -> Any:
        return stale.post(
            AUTH + "auth/email/verify/resend",
            {},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )

    def verify() -> Any:
        return submit(client, "auth/email/verify", {"key": mailed_code()})

    if order == "processed before":
        late = resend()
        signing_in = verify()
        assert late.status_code == 200
    else:
        signing_in = verify()
        late = resend()
        assert (late.status_code, late.json()["code"]) == (409, "conflict")
    assert signing_in.status_code == 200
    jar = browser()
    jar.cookies[settings.SESSION_COOKIE_NAME] = old

    arrive(jar, signing_in, late)

    assert session_key(jar) == session_key(client) != old
    assert session_user(jar) == user.pk
    assert not Session.objects.filter(session_key=old).exists()
