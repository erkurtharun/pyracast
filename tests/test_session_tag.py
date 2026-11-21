import logging
from collections.abc import Iterator
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import BACKEND_SESSION_KEY
from django.contrib.auth.models import User
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from tests.support.account_sessions import (
    account,
    browser,
    capture_security_events,
    carrying_key,
    session_key,
    session_user,
)
from tests.support.auth_lifecycle import AUTH, PASSWORD, submit

pytestmark = pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)

CHANGED_PASSWORD = PASSWORD + "-changed"


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


def read(client: Client) -> dict[str, Any]:
    response = client.get("/api/session/")
    assert response.status_code == 200
    return dict(response.json())


def tag_of(client: Client) -> str | None:
    tag = read(client)["session_tag"]
    assert tag is None or isinstance(tag, str)
    return tag


def sign_in(client: Client, user: User) -> None:
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 200


def tagged(client: Client, method: str, path: str, tag: str, data: Any = None) -> Any:
    token = read(client)["csrf_token"]
    return getattr(client, method)(
        path,
        data if data is not None else {},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
        HTTP_X_SESSION_TAG=tag,
    )


def test_a_sign_in_gives_the_session_a_tag_that_the_session_read_returns() -> None:
    user = account("owner")
    client = browser()
    assert tag_of(client) is None

    sign_in(client, user)
    first = tag_of(client)
    assert first is not None
    assert len(first) >= 22
    assert tag_of(client) == first

    token = read(client)["csrf_token"]
    client.delete(AUTH + "auth/session", HTTP_X_CSRFTOKEN=token)
    assert tag_of(client) is None
    sign_in(client, user)
    again = tag_of(client)
    assert again is not None
    assert again != first


def test_every_session_signed_in_has_its_own_tag() -> None:
    user = account("owner")
    first, second = browser(), browser()
    sign_in(first, user)
    sign_in(second, user)

    assert tag_of(first) != tag_of(second)


def test_a_confirmation_that_renews_the_key_keeps_the_tag() -> None:
    user = account("owner")
    client = browser()
    sign_in(client, user)
    tag, key = tag_of(client), session_key(client)

    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200

    assert session_key(client) != key
    assert tag_of(client) == tag


def test_a_signed_in_session_without_a_tag_gets_one_at_its_session_read() -> None:
    from pyracast.accounts.session_tag import TAG

    user = account("owner")
    client = browser()
    sign_in(client, user)
    store = SessionStore(session_key(client))
    store.pop(TAG)
    store.save()

    tag = tag_of(client)

    assert tag is not None
    assert tag_of(client) == tag
    assert SessionStore(session_key(client))[TAG] == tag


ROUTES = {
    "password change": ("post", AUTH + "account/password/change", "password"),
    "sign-out": ("delete", AUTH + "auth/session", None),
    "address change": ("post", AUTH + "account/email", {"email": "new@example.com"}),
    "authenticator read": ("get", AUTH + "account/authenticators", None),
    "confirmation": ("post", AUTH + "auth/reauthenticate", {"password": PASSWORD}),
    "session list": ("get", AUTH + "auth/sessions", None),
    "language": ("post", "/api/session/language/", {"language": "tr"}),
    "workspaces": ("get", "/api/tenants/", None),
    "workspace write": ("post", "/api/tenants/1/sites/", {"name": "Site"}),
    "workspace removal": ("delete", "/api/tenants/1/members/2/", None),
    "workspace update": ("put", "/api/tenants/1/regions/1/collection-schedule/", {}),
}


@pytest.mark.parametrize("route", ROUTES)
def test_a_request_prepared_for_another_session_is_refused_before_any_view_or_write(
    route: str,
) -> None:
    user = account("owner")
    client = browser()
    sign_in(client, user)
    method, path, data = ROUTES[route]
    if data == "password":
        data = {"current_password": PASSWORD, "new_password": CHANGED_PASSWORD}
    token = read(client)["csrf_token"]

    with CaptureQueriesContext(connection) as queries:
        response = getattr(client, method)(
            path,
            data or {},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
            HTTP_X_SESSION_TAG="tag-of-an-earlier-session",
        )

    assert (response.status_code, response.json()["code"]) == (409, "session_changed")
    statements = {query["sql"].lstrip().split()[0].upper() for query in queries.captured_queries}
    assert not statements & {"INSERT", "UPDATE", "DELETE"}
    assert session_user(client) == user.pk
    assert User.objects.get(pk=user.pk).check_password(PASSWORD)
    assert not EmailAddress.objects.filter(email="new@example.com").exists()


def test_a_request_carrying_its_session_tag_reaches_the_view() -> None:
    user = account("owner")
    client = browser()
    sign_in(client, user)

    response = tagged(client, "get", AUTH + "account/authenticators", str(tag_of(client)))

    assert response.status_code == 200


def test_the_session_read_answers_whatever_tag_a_request_carries() -> None:
    user = account("owner")
    client = browser()
    sign_in(client, user)
    tag = tag_of(client)

    response = client.get("/api/session/", HTTP_X_SESSION_TAG="tag-of-an-earlier-session")

    assert response.status_code == 200
    assert response.json()["session_tag"] == tag
    assert response.json()["user"]["id"] == user.pk


def test_a_request_without_a_tag_is_not_compared() -> None:
    user = account("owner")
    client = browser()
    sign_in(client, user)
    token = read(client)["csrf_token"]

    response = client.get(AUTH + "account/authenticators", HTTP_X_CSRFTOKEN=token)

    assert response.status_code == 200


def test_a_sign_in_prepared_before_another_tab_signed_in_changes_nothing() -> None:
    first, second = account("first"), account("second")
    client = browser()
    sign_in(client, first)

    refused = tagged(
        client,
        "post",
        AUTH + "auth/login",
        "",
        {"email": second.email, "password": PASSWORD},
    )

    assert (refused.status_code, refused.json()["code"]) == (409, "session_changed")
    assert session_user(client) == first.pk


def test_an_anonymous_tab_signs_in_with_the_empty_tag_of_a_browser_without_a_session() -> None:
    user = account("owner")
    client = browser()

    response = tagged(
        client, "post", AUTH + "auth/login", "", {"email": user.email, "password": PASSWORD}
    )

    assert response.status_code == 200
    assert session_user(client) == user.pk


def test_a_sign_out_prepared_for_an_earlier_session_ends_nothing_and_the_current_one_can_end() -> (
    None
):
    first, second = account("first"), account("second")
    client = browser()
    sign_in(client, first)
    earlier = str(tag_of(client))
    token = read(client)["csrf_token"]
    client.delete(AUTH + "auth/session", HTTP_X_CSRFTOKEN=token)
    sign_in(client, second)

    refused = tagged(client, "delete", AUTH + "auth/session", earlier)

    assert (refused.status_code, refused.json()["code"]) == (409, "session_changed")
    assert session_user(client) == second.pk
    current = str(tag_of(client))
    assert tagged(client, "delete", AUTH + "auth/session", current).status_code == 401
    assert session_user(client) is None


def test_a_request_under_a_key_a_confirmation_replaced_is_refused_and_runs_nothing() -> None:
    user = account("owner")
    client = browser()
    sign_in(client, user)
    tag = str(tag_of(client))
    replaced = carrying_key(session_key(client))
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200

    response = tagged(replaced, "post", AUTH + "account/email", tag, {"email": "new@example.com"})

    assert (response.status_code, response.json()["code"]) == (409, "session_changed")
    assert not EmailAddress.objects.filter(email="new@example.com").exists()
    assert tag_of(client) == tag


def switch_off(user: User, _client: Client) -> None:
    User.objects.filter(pk=user.pk).update(is_active=False)


def remove(user: User, _client: Client) -> None:
    user.delete()


def change_credentials(user: User, _client: Client) -> None:
    user.set_password(CHANGED_PASSWORD)
    user.save(update_fields=["password"])


def unlist_backend(_user: User, client: Client) -> None:
    store = SessionStore(session_key(client))
    store[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    store.save()


SIGNED_OUT_STATES = {
    "account switched off": switch_off,
    "account removed": remove,
    "credentials changed": change_credentials,
    "sign-in backend no longer listed": unlist_backend,
}
ENTRIES = {
    "sign-in": (
        "post",
        AUTH + "auth/login",
        {"email": "second@example.com", "password": PASSWORD},
        200,
    ),
    "sign-up": (
        "post",
        AUTH + "auth/signup",
        {"email": "fresh@example.com", "password": PASSWORD, "company_name": "Solar"},
        401,
    ),
    "password reset request": (
        "post",
        AUTH + "auth/password/request",
        {"email": "owner@example.com"},
        401,
    ),
    "language": ("post", "/api/session/language/", {"language": "tr"}, 200),
    "sign-out": ("delete", AUTH + "auth/session", None, 401),
}


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("state", SIGNED_OUT_STATES)
def test_a_browser_whose_session_signs_in_no_account_holds_the_empty_tag(
    state: str, entry: str
) -> None:
    user = account("owner")
    second = account("second")
    client = browser()
    sign_in(client, user)
    earlier = str(tag_of(client))
    SIGNED_OUT_STATES[state](user, client)

    assert read(client)["user"] is None
    assert tag_of(client) is None
    method, path, data, status = ENTRIES[entry]
    response = tagged(client, method, path, "", data)

    assert response.status_code == status
    assert session_user(client) == (second.pk if entry == "sign-in" else None)
    refused = tagged(client, "get", "/api/tenants/", earlier)
    assert (refused.status_code, refused.json()["code"]) == (409, "session_changed")


def test_the_tag_authorizes_nothing() -> None:
    user = account("owner")
    client = browser()
    sign_in(client, user)
    tag = str(tag_of(client))
    stranger = browser()

    response = tagged(stranger, "get", "/api/tenants/", tag)

    assert (response.status_code, response.json()["code"]) == (409, "session_changed")
    anonymous = tagged(stranger, "get", "/api/tenants/", "")
    assert anonymous.json()["code"] == "not_authenticated"


def test_the_tag_appears_in_no_log_line_and_no_address(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    security_lines = capture_security_events(caplog, monkeypatch)
    user = account("owner")
    client = browser()
    sign_in(client, user)
    tag = str(tag_of(client))
    answers = [
        tagged(client, "get", "/api/tenants/", "tag-of-an-earlier-session"),
        tagged(client, "post", AUTH + "auth/reauthenticate", tag, {"password": PASSWORD}),
        tagged(client, "delete", AUTH + "auth/session", tag),
    ]

    assert security_lines()
    assert all(tag not in record.getMessage() for record in caplog.records)
    assert all(tag not in answer.get("Location", "") for answer in answers)
    assert all(tag not in str(answer.cookies) for answer in answers)
