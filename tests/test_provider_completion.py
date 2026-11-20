import json
import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import requests
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth.models import User
from django.core import mail
from django.db import DatabaseError, connections
from django.test import Client, override_settings

from pyracast.accounts import processes
from pyracast.accounts.audit import logger
from tests.support.account_sessions import (
    account,
    browser,
    elsewhere,
    session_cookie,
    session_user,
    signed_in,
)
from tests.support.auth_lifecycle import AUTH, PASSWORD, submit
from tests.support.social_accounts import (
    CALLBACK,
    PROVIDERS,
    begin,
    callback,
    complete,
    finish,
    serve_provider_profile,
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


@pytest.fixture
def profile(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    served = serve_provider_profile(monkeypatch)
    with override_settings(
        SOCIALACCOUNT_PROVIDERS=PROVIDERS,
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
    ):
        yield served


@pytest.fixture
def lines(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], list[str]]:
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    caplog.set_level(logging.INFO, logger="pyracast.security")
    return lambda: [
        json.loads(record.getMessage())["event"]
        for record in caplog.records
        if record.name == "pyracast.security"
    ]


def connecting(name: str, *, identity: str | None = None) -> tuple[User, Client, str]:
    user = account(name)
    if identity is not None:
        SocialAccount.objects.create(user=user, provider="google", uid=identity)
    client = signed_in(user, reauthenticated=True)
    return user, client, begin(client, process="connect")


def error_codes(response: Any) -> list[str]:
    return [error["code"] for error in response.json()["errors"]]


@pytest.mark.usefixtures("profile")
def test_the_callback_verifies_the_login_and_signs_nobody_in() -> None:
    user = account("owner")
    SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    client = browser()

    verified = callback(client, begin(client))

    assert (verified.status_code, verified["Location"]) == (302, CALLBACK)
    assert session_cookie(verified) is None
    assert session_user(client) is None
    completed = finish(client)
    assert completed.status_code == 200
    assert completed.json()["data"]["user"]["id"] == user.pk
    assert session_user(client) == user.pk


@pytest.mark.usefixtures("profile")
def test_the_callback_connects_nothing() -> None:
    user, client, state = connecting("owner")

    verified = callback(client, state)

    assert (verified.status_code, verified["Location"]) == (302, CALLBACK)
    assert session_cookie(verified) is None
    assert not SocialAccount.objects.exists()
    connected = finish(client)
    assert connected.status_code == 200
    assert [listed["uid"] for listed in connected.json()["data"]] == ["google-identity"]
    assert SocialAccount.objects.get().user_id == user.pk


def expire_stored_logins() -> None:
    with connections["auth_cache"].cursor() as cursor:
        cursor.execute(
            "UPDATE pyracast_authentication_cache SET expires = clock_timestamp() "
            "- interval '1 second' WHERE value ? 'account'"
        )


def other_browser(client: Client) -> Client:
    return browser()


def signed_out(client: Client) -> Client:
    token = client.get("/api/session/").json()["csrf_token"]
    assert client.delete(AUTH + "auth/session", HTTP_X_CSRFTOKEN=token).status_code == 401
    return client


def password_changed(client: Client) -> Client:
    data = {"current_password": PASSWORD, "new_password": PASSWORD + "-changed"}
    assert submit(client, "account/password/change", data).status_code == 401
    return client


def expired(client: Client) -> Client:
    expire_stored_logins()
    return client


def completed_once(client: Client) -> Client:
    assert finish(client).status_code == 200
    return client


WITHOUT_A_STORED_LOGIN: dict[str, Callable[[Client], Client]] = {
    "another-browser": other_browser,
    "signed-out": signed_out,
    "password-changed": password_changed,
    "expired": expired,
    "completed-once": completed_once,
}


@pytest.mark.usefixtures("profile")
@pytest.mark.parametrize("case", WITHOUT_A_STORED_LOGIN)
def test_a_completion_without_its_stored_login_is_refused_and_changes_nothing(
    case: str,
) -> None:
    _user, client, state = connecting("owner")
    assert callback(client, state)["Location"] == CALLBACK
    client = WITHOUT_A_STORED_LOGIN[case](client)
    identities = list(SocialAccount.objects.values_list("user_id", "uid"))
    mail.outbox.clear()

    response = finish(client)

    assert response.status_code == 409
    assert list(SocialAccount.objects.values_list("user_id", "uid")) == identities
    assert mail.outbox == []


@pytest.mark.usefixtures("profile")
def test_a_completion_from_another_site_is_refused_and_keeps_the_stored_login() -> None:
    user, client, state = connecting("owner")
    assert callback(client, state)["Location"] == CALLBACK

    forged = client.post(AUTH + "auth/provider/complete", {}, content_type="application/json")

    assert forged.status_code == 403
    assert not SocialAccount.objects.exists()
    assert finish(client).status_code == 200
    assert SocialAccount.objects.get().user_id == user.pk


def test_a_connection_refused_after_the_provider_exchange_names_the_factor_to_confirm(
    profile: dict[str, Any],
) -> None:
    user, client, state = connecting("owner")

    def authenticator_added() -> None:
        elsewhere(lambda: TOTP.activate(user, generate_totp_secret()))

    profile["_before_profile"] = authenticator_added
    assert callback(client, state)["Location"] == CALLBACK

    response = finish(client)

    assert response.status_code == 401
    assert [flow["id"] for flow in response.json()["data"]["flows"]] == ["mfa_reauthenticate"]
    assert error_codes(response) == ["mfa_reauthentication_required"]
    assert not SocialAccount.objects.exists()


def test_a_refused_connection_keeps_a_pending_provider_signup(profile: dict[str, Any]) -> None:
    owner = account("owner")
    SocialAccount.objects.create(user=owner, provider="google", uid="owned-elsewhere")
    client = browser()
    profile["sub"] = "new-identity"
    state = begin(client)
    callback(client, state)
    finish(client)
    assert "socialaccount_sociallogin" in client.session
    user = account("member")
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 200
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    assert "socialaccount_sociallogin" in client.session
    state = begin(client, process="connect")
    profile["sub"] = "owned-elsewhere"
    callback(client, state)

    response = finish(client)

    assert "socialaccount_sociallogin" in client.session
    assert (response.status_code, error_codes(response)) == (400, ["connected_other"])
    assert SocialAccount.objects.get().user_id == owner.pk


def test_connections_of_each_others_identities_are_both_refused(
    profile: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, first_client, first_state = connecting("first", identity="identity-first")
    second, second_client, second_state = connecting("second", identity="identity-second")
    served = threading.local()
    request = requests.Session.request
    serving = threading.Lock()

    def per_thread(self: requests.Session, method: str, url: str, *a: Any, **k: Any) -> Any:
        with serving:
            profile["sub"] = served.uid
            return request(self, method, url, *a, **k)

    monkeypatch.setattr(requests.Session, "request", per_thread)
    # Both requests hold their own account's locks before either reads the other's identity.
    barrier = threading.Barrier(2)
    state = processes.account_state
    met: set[int] = set()

    def locked_then_met(user_id: Any, *, lock: bool = False) -> str | None:
        current = state(user_id, lock=lock)
        thread = threading.get_ident()
        if lock and getattr(served, "uid", None) and thread not in met:
            met.add(thread)
            barrier.wait(timeout=10)
        return current

    monkeypatch.setattr(processes, "account_state", locked_then_met)
    answers: dict[str, tuple[Any, Any]] = {}

    def connect(name: str, client: Client, state: str, uid: str) -> None:
        served.uid = uid
        client.raise_request_exception = False
        try:
            answers[name] = (callback(client, state), finish(client))
        finally:
            connections.close_all()

    threads = [
        threading.Thread(
            target=connect, args=("first", first_client, first_state, "identity-second")
        ),
        threading.Thread(
            target=connect, args=("second", second_client, second_state, "identity-first")
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert {name: verified.status_code for name, (verified, _) in answers.items()} == {
        "first": 302,
        "second": 302,
    }
    assert {
        name: (completed.status_code, error_codes(completed))
        for name, (_, completed) in answers.items()
    } == {"first": (400, ["connected_other"]), "second": (400, ["connected_other"])}
    assert set(SocialAccount.objects.values_list("user_id", "uid")) == {
        (first.pk, "identity-first"),
        (second.pk, "identity-second"),
    }


def test_a_connection_that_rolls_back_leaves_no_line_and_no_mail(
    profile: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    lines: Callable[[], list[str]],
) -> None:
    _user, client, state = connecting("owner")
    callback(client, state)
    connect = SocialLogin.connect

    def connected_then_failed(login: SocialLogin, request: Any, user: User) -> None:
        connect(login, request, user)
        raise DatabaseError("The transaction failed after the connection.")

    monkeypatch.setattr(SocialLogin, "connect", connected_then_failed)
    mail.outbox.clear()
    client.raise_request_exception = False

    response = finish(client)

    assert response.status_code >= 500
    assert not SocialAccount.objects.exists()
    assert "social_account_connected" not in lines()
    assert mail.outbox == []


@pytest.mark.usefixtures("profile")
def test_a_connection_writes_its_line_and_mail_with_its_commit(
    lines: Callable[[], list[str]],
) -> None:
    _user, client, state = connecting("owner")
    mail.outbox.clear()

    response = complete(client, state)

    assert response.status_code == 200
    assert lines().count("social_account_connected") == 1
    assert [message.extra_headers["X-Pyracast-Mail"] for message in mail.outbox] == [
        "socialaccount/email/account_connected"
    ]


def test_a_connection_of_its_own_identity_refreshes_its_profile(
    profile: dict[str, Any],
) -> None:
    user, client, state = connecting("owner", identity="google-identity")
    SocialAccount.objects.update(extra_data={"email": "old@gmail.com"})

    response = complete(client, state)

    assert response.status_code == 200
    assert list(SocialAccount.objects.values_list("user_id", "extra_data")) == [
        (user.pk, {"email": profile["email"]})
    ]
