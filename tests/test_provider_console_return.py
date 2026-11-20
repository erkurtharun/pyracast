import time
from collections.abc import Iterator
from urllib.parse import parse_qs, urlsplit

import pytest
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth.models import User
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, override_settings

from tests.support.account_sessions import browser, session_key, totp_clock, with_totp
from tests.support.auth_lifecycle import PASSWORD, submit, totp_code
from tests.support.social_accounts import (
    AUTH,
    CALLBACK,
    PROVIDERS,
    callback,
    finish,
    provider_destination,
    serve_provider_profile,
)

pytestmark = pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)

DEEP_LINK = "/admin/plants/7/?tab=review"


@pytest.fixture(autouse=True)
def providers() -> Iterator[None]:
    with override_settings(
        SOCIALACCOUNT_PROVIDERS=PROVIDERS,
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
    ):
        yield


def operator() -> tuple[User, str]:
    user = User.objects.create_user(
        username="operator",
        email="operator@example.com",
        password=PASSWORD,
        is_staff=True,
        is_superuser=True,
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    return user, with_totp(user)


def begin_with(client: Client, target: str) -> str:
    token = client.get("/api/session/").json()["csrf_token"]
    response = client.post(
        AUTH + "auth/provider/redirect",
        {"provider": "google", "process": "login", "callback_url": CALLBACK, "next": target},
        HTTP_X_CSRFTOKEN=token,
    )
    return parse_qs(urlsplit(provider_destination(response)).query)["state"][0]


def signed_in_through_the_provider(
    client: Client, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, secret = operator()
    serve_provider_profile(monkeypatch)
    state = begin_with(client, target)
    verified = callback(client, state)
    assert (verified.status_code, verified["Location"]) == (302, CALLBACK)
    pending = finish(client)
    assert pending.status_code == 401
    now = time.time()
    totp_clock(monkeypatch, now)
    assert (
        submit(client, "auth/2fa/authenticate", {"code": totp_code(secret, now)}).status_code == 200
    )


def test_a_console_deep_link_is_the_landing_after_a_provider_sign_in_and_its_second_factor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = browser()
    signed_in_through_the_provider(client, DEEP_LINK, monkeypatch)

    landing = client.get("/admin/")

    assert (landing.status_code, landing.get("Location")) == (302, DEEP_LINK)
    assert client.get("/admin/").status_code == 200


@pytest.mark.parametrize(
    "target", ["https://elsewhere.example/admin/", "//elsewhere.example/admin/", "/app/w/"]
)
def test_a_return_outside_the_console_is_not_kept(
    target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = browser()
    signed_in_through_the_provider(client, target, monkeypatch)

    assert client.get("/admin/").status_code == 200


def test_a_return_in_the_provider_callback_address_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _user, secret = operator()
    serve_provider_profile(monkeypatch)
    client = browser()
    state = begin_with(client, "")
    verified = client.get(
        "/api/accounts/google/login/callback/",
        {"state": state, "code": "provider-code", "next": DEEP_LINK},
    )
    assert verified["Location"] == CALLBACK
    assert finish(client).status_code == 401
    now = time.time()
    totp_clock(monkeypatch, now)
    assert (
        submit(client, "auth/2fa/authenticate", {"code": totp_code(secret, now)}).status_code == 200
    )

    assert client.get("/admin/").status_code == 200


def test_a_stored_return_is_checked_again_before_the_console_follows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyracast.platform_admin.navigation import CONSOLE_RETURN

    client = browser()
    signed_in_through_the_provider(client, "", monkeypatch)
    store = SessionStore(session_key(client))
    store[CONSOLE_RETURN] = "https://elsewhere.example/admin/"
    store.save()

    assert client.get("/admin/").status_code == 200
    assert CONSOLE_RETURN not in SessionStore(session_key(client))
