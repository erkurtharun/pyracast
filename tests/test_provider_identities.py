from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from allauth.account.internal.flows.email_verification import (
    consume_email_verification_rate_limit,
)
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth.models import User
from django.core import mail
from django.db import connection
from django.test import Client, RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tests.support.account_sessions import (
    account,
    answer,
    browser,
    capture_security_events,
    session_user,
    signed_in,
)
from tests.support.auth_lifecycle import AUTH, PASSWORD, mailed_code, submit
from tests.support.social_accounts import (
    CALLBACK,
    PROVIDERS,
    begin,
    complete,
    provider_destination,
    serve_provider_profile,
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


@pytest.fixture(autouse=True)
def social_settings() -> Iterator[None]:
    with override_settings(
        SOCIALACCOUNT_PROVIDERS=PROVIDERS,
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
    ):
        yield


@pytest.fixture
def profile(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return serve_provider_profile(monkeypatch)


def pending_signup(provider: str) -> Client:
    client = browser()
    assert complete(client, begin(client, provider), provider)["Location"] == CALLBACK
    return client


def sent() -> list[tuple[str, list[str]]]:
    return [(message.extra_headers["X-Pyracast-Mail"], message.to) for message in mail.outbox]


@pytest.mark.usefixtures("profile")
@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_a_provider_signup_with_another_address_stores_and_verifies_only_that_address(
    provider: str,
) -> None:
    client = pending_signup(provider)

    response = submit(
        client, "auth/provider/signup", {"email": "typed@example.com", "company_name": "Solar"}
    )

    assert response.status_code == 401
    user = User.objects.get()
    rows = EmailAddress.objects.filter(user=user).values_list("email", "primary", "verified")
    assert list(rows) == [("typed@example.com", True, False)]
    assert sent() == [("account/email/email_confirmation_signup", ["typed@example.com"])]
    assert submit(client, "auth/email/verify", {"key": mailed_code()}).status_code == 200
    assert session_user(client) == user.pk


def test_a_provider_signup_answers_alike_whether_or_not_the_typed_address_has_an_account(
    profile: dict[str, Any],
) -> None:
    answers = []
    for provider in ("google", "microsoft"):
        known = account(f"{provider}-member").email
        for typed in (f"{provider}-new@example.com", known):
            profile["sub"] = profile["id"] = f"identity-for-{typed}"
            client = pending_signup(provider)
            details = {"email": typed, "company_name": "Solar"}
            answers.append(answer(submit(client, "auth/provider/signup", details)))

    assert answers[0][0] == 401
    assert answers == [answers[0]] * 4


@pytest.mark.usefixtures("profile")
def test_a_provider_signup_that_meets_the_address_mail_limit_records_one_throttle(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = pending_signup("microsoft")
    assert consume_email_verification_rate_limit(RequestFactory().post("/"), "typed@example.com")
    events = capture_security_events(caplog, monkeypatch)

    response = submit(
        client, "auth/provider/signup", {"email": "typed@example.com", "company_name": "Solar"}
    )

    throttled = [
        event for event in events() if event["event"] == "authentication_request_throttled"
    ]
    assert (response.status_code, len(throttled)) == (429, 1)
    assert mail.outbox == []


@pytest.mark.usefixtures("profile")
def test_provider_sign_in_while_signed_in_is_refused_and_keeps_the_session() -> None:
    user = account("owner")
    client = signed_in(user)

    response = complete(client, begin(client, "google"), "google")

    assert "error=permission_denied" in response["Location"]
    assert session_user(client) == user.pk
    assert "socialaccount_sociallogin" not in client.session
    assert not SocialAccount.objects.exists()


def test_a_password_reset_of_a_never_used_account_unbinds_its_provider_identities(
    profile: dict[str, Any],
) -> None:
    profile["mail"] = "victim@example.com"
    claimant = pending_signup("microsoft")
    details = {"email": "victim@example.com", "company_name": "Solar"}
    assert submit(claimant, "auth/provider/signup", details).status_code == 401
    victim = browser()
    request = {"email": "victim@example.com"}
    assert submit(victim, "auth/password/request", request).status_code == 401
    reset = {"key": mailed_code(), "password": PASSWORD + "victim"}
    assert submit(victim, "auth/password/reset", reset).status_code == 401

    returning = browser()
    response = complete(returning, begin(returning, "microsoft"), "microsoft")

    assert response["Location"] == CALLBACK
    assert session_user(returning) is None
    flows = returning.get(AUTH + "auth/session").json()["data"]["flows"]
    assert any(flow["id"] == "provider_signup" for flow in flows)
    assert not SocialAccount.objects.exists()
    credentials = {"email": "victim@example.com", "password": PASSWORD + "victim"}
    assert submit(browser(), "auth/login", credentials).status_code == 200


def test_a_password_reset_of_a_used_account_keeps_its_provider_identities() -> None:
    user = account("owner")
    User.objects.filter(pk=user.pk).update(last_login=timezone.now())
    SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    client = browser()
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    reset = {"key": mailed_code(), "password": PASSWORD + "reset"}

    assert submit(client, "auth/password/reset", reset).status_code == 401

    assert SocialAccount.objects.filter(user=user).exists()


def test_disconnecting_a_provider_ends_the_other_sessions_of_the_account() -> None:
    user = account("owner")
    SocialAccount.objects.create(
        user=user, provider="google", uid="google-identity", extra_data={"email": user.email}
    )
    current = signed_in(user, reauthenticated=True)
    other = signed_in(user)
    token = current.get("/api/session/").json()["csrf_token"]

    removed = current.delete(
        AUTH + "account/providers",
        {"provider": "google", "account": "google-identity"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )

    assert removed.status_code == 200
    assert session_user(other) is None
    assert session_user(current) == user.pk


@pytest.mark.usefixtures("profile")
def test_a_known_provider_identity_signing_in_stores_only_its_address() -> None:
    user = account("owner")
    SocialAccount.objects.create(
        user=user,
        provider="microsoft",
        uid="microsoft-identity",
        extra_data={"email": "solar@example.com"},
    )
    client = browser()
    state = begin(client, "microsoft")

    with CaptureQueriesContext(connection) as queries:
        complete(client, state, "microsoft")

    assert not any("Private profile detail" in query["sql"] for query in queries.captured_queries)
    assert SocialAccount.objects.get().extra_data == {"email": "solar@example.com"}
    assert session_user(client) == user.pk


def test_a_connection_to_an_unknown_provider_is_listed_without_sign_in_flows() -> None:
    user = account("owner")
    SocialAccount.objects.create(
        user=user, provider="retired", uid="retired-identity", extra_data={"email": user.email}
    )

    response = signed_in(user).get(AUTH + "account/providers")

    assert response.status_code == 200
    (listed,) = response.json()["data"]
    assert (listed["uid"], listed["provider"]["id"]) == ("retired-identity", "retired")
    assert listed["provider"]["flows"] == []


def test_a_provider_sign_in_carries_no_initiating_user() -> None:
    client = signed_in(account("owner"))

    state = begin(client, "google")

    assert client.session["socialaccount_states"][state][0]["data"] == {}


def test_provider_redirect_uri_uses_https_outside_debug() -> None:
    client = browser()
    token = client.get("/api/session/").json()["csrf_token"]
    response = client.post(
        AUTH + "auth/provider/redirect",
        {"provider": "google", "process": "login", "callback_url": CALLBACK},
        HTTP_X_CSRFTOKEN=token,
    )

    query = parse_qs(urlsplit(provider_destination(response)).query)

    assert query["redirect_uri"] == ["https://testserver/api/accounts/google/login/callback/"]
