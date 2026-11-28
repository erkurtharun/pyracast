import runpy
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from typing import Any
from urllib.parse import urlencode

import pytest
import requests
from allauth.account.models import EmailAddress
from allauth.core import context
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.headless.socialaccount.inputs import SignupInput
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import generate_totp_secret
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin, SocialToken
from allauth.socialaccount.providers.google.provider import GoogleProvider
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.test import Client, override_settings

from pyracast.accounts import processes
from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import Membership, Tenant, UserLanguagePreference, WorkspaceRole
from pyracast.accounts.social import SocialAccountAdapter
from tests.support.auth_lifecycle import PASSWORD, member, submit
from tests.support.company_registration import mailed_code
from tests.support.social_accounts import (
    AUTH,
    CALLBACK,
    PROVIDERS,
    begin,
    callback,
    complete,
    provider_destination,
    serve_provider_profile,
)
from tests.support.workspaces import invitation_token

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


@pytest.fixture(autouse=True)
def social_settings() -> Iterator[None]:
    with override_settings(
        SOCIALACCOUNT_PROVIDERS=PROVIDERS,
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
    ):
        yield


@pytest.fixture
def oauth_profile(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return serve_provider_profile(monkeypatch)


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_provider_authorization_uses_html_continuation(provider: str) -> None:
    client = Client(enforce_csrf_checks=True)
    assert client.get(AUTH + "auth/provider/redirect").status_code == 405
    begin(client, provider)


def test_provider_continuation_escapes_framework_destination_and_preserves_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = 'https://accounts.google.com/authorize?state=a&value="<script>alert(1)</script>'
    provider_response = HttpResponseRedirect(destination)
    provider_response["Location"] = destination
    provider_response.set_cookie("provider-contract", "retained", httponly=True, secure=True)
    provider_response["X-Provider-Contract"] = "retained"

    def redirect(self: object, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return provider_response

    monkeypatch.setattr(GoogleProvider, "redirect", redirect)
    response = Client().post(
        AUTH + "auth/provider/redirect",
        {
            "provider": "google",
            "process": "login",
            "callback_url": CALLBACK,
            "next": "https://evil.example/",
        },
    )
    assert provider_destination(response) == provider_response.url
    assert response.cookies["provider-contract"] == provider_response.cookies["provider-contract"]
    assert response["X-Provider-Contract"] == "retained"
    assert b"&amp;" in response.content
    assert b"&quot;&lt;script&gt;" in response.content
    assert b"<script>" not in response.content


@pytest.mark.parametrize("destination", ["/error/", CALLBACK, "http://testserver/error/"])
def test_provider_local_redirects_are_preserved(
    monkeypatch: pytest.MonkeyPatch, destination: str
) -> None:
    def redirect(self: object, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return HttpResponseRedirect(destination)

    monkeypatch.setattr(GoogleProvider, "redirect", redirect)
    response = Client().post(
        AUTH + "auth/provider/redirect",
        {"provider": "google", "process": "login", "callback_url": CALLBACK},
    )
    assert response.status_code == 302
    assert response["Location"] == destination


def test_provider_nonredirect_response_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    def redirect(self: object, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return HttpResponse("Provider unavailable", status=503)

    monkeypatch.setattr(GoogleProvider, "redirect", redirect)
    response = Client().post(
        AUTH + "auth/provider/redirect",
        {"provider": "google", "process": "login", "callback_url": CALLBACK},
    )
    assert response.status_code == 503
    assert response.content == b"Provider unavailable"


def test_config_enables_only_deployment_providers_and_redirects() -> None:
    SocialApp.objects.create(provider="google", client_id="database-client", secret="db-secret")  # noqa: S106
    client = Client()
    providers = client.get(AUTH + "config").json()["data"]["socialaccount"]["providers"]
    assert [provider["id"] for provider in providers] == ["google", "microsoft"]
    assert all(provider["flows"] == ["provider_redirect"] for provider in providers)
    assert [provider["client_id"] for provider in providers] == [
        "google-client",
        "microsoft-client",
    ]
    with override_settings(SOCIALACCOUNT_PROVIDERS={}):
        assert client.get(AUTH + "config").json()["data"]["socialaccount"]["providers"] == []


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_provider_signup_creates_workspace_atomically(
    oauth_profile: dict[str, Any], provider: str
) -> None:
    client = Client(enforce_csrf_checks=True)
    state = begin(client, provider)
    response = complete(client, state, provider)
    assert response.status_code == 401
    assert any(
        flow["id"] == "provider_signup" and flow.get("is_pending")
        for flow in response.json()["data"]["flows"]
    )
    session = client.get(AUTH + "auth/session").json()
    assert any(flow["id"] == "provider_signup" for flow in session["data"]["flows"])
    assert client.get(AUTH + "auth/webauthn/login?autofill").status_code == 409
    pending = client.session["socialaccount_sociallogin"]
    assert "token" not in pending
    assert pending["account"]["extra_data"] == {
        "email": oauth_profile["email"] if provider == "google" else oauth_profile["mail"]
    }
    assert not User.objects.exists()
    assert (
        client.post(AUTH + "auth/provider/signup", {}, content_type="application/json").status_code
        == 403
    )
    prefill = client.get(AUTH + "auth/provider/signup").json()["data"]
    assert prefill["email"][0]["email"] == (
        oauth_profile["email"] if provider == "google" else oauth_profile["mail"]
    )
    invalid = submit(client, "auth/provider/signup", {"email": prefill["email"][0]["email"]})
    assert invalid.status_code == 400
    assert not User.objects.exists()
    client.cookies["pyracast_language"] = "tr"
    result = submit(
        client,
        "auth/provider/signup",
        {"email": prefill["email"][0]["email"], "company_name": "Social Solar"},
    )
    assert result.status_code == (200 if provider == "google" else 401)
    user = User.objects.get()
    assert not user.has_usable_password()
    assert not user.is_staff
    assert UserLanguagePreference.objects.get(user=user).language == "tr"
    assert Membership.objects.get(user=user).tenant.name == "Social Solar"
    assert Membership.objects.get(user=user).role == WorkspaceRole.ADMIN
    assert not SocialToken.objects.exists()
    assert SocialAccount.objects.get(user=user).provider == provider
    if provider == "microsoft":
        assert not EmailAddress.objects.get(user=user).verified
        assert submit(client, "auth/email/verify", {"key": mailed_code()}).status_code == 200
    assert client.get("/api/session/").json()["user"]["id"] == user.pk
    assert client.get(AUTH + "account/providers").json()["data"][0]["display"] == user.email


@pytest.mark.parametrize(
    ("email", "hd", "verified"),
    [
        ("owner@gmail.com", "", True),
        ("owner@company.example", "company.example", True),
        ("owner@external.example", "", False),
    ],
)
def test_google_email_authority_is_not_assumed_for_external_addresses(
    oauth_profile: dict[str, Any], email: str, hd: str, verified: bool
) -> None:
    oauth_profile.update(email=email, hd=hd)
    client = Client()
    complete(client, begin(client))
    assert (
        client.get(AUTH + "auth/provider/signup").json()["data"]["email"][0]["verified"] is verified
    )


def test_provider_never_connects_by_matching_email(oauth_profile: dict[str, Any]) -> None:
    user = member()
    oauth_profile["email"] = user.email
    client = Client()
    complete(client, begin(client))
    response = submit(
        client, "auth/provider/signup", {"email": user.email, "company_name": "Other"}
    )
    assert response.status_code in {400, 401}
    assert not SocialAccount.objects.exists()
    assert not Tenant.objects.exists()
    assert client.get("/api/session/").json()["user"] is None


def test_social_link_and_unlink_require_reauthentication_and_emit_audit(
    oauth_profile: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    user = member()
    client = Client(enforce_csrf_checks=True)
    assert (
        submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 200
    )
    token = client.get("/api/session/").json()["csrf_token"]
    data = {"provider": "google", "process": "connect", "callback_url": CALLBACK}
    assert client.post(AUTH + "auth/provider/redirect", data).status_code == 403
    denied = client.post(AUTH + "auth/provider/redirect", data, HTTP_X_CSRFTOKEN=token)
    assert denied.status_code == 302
    assert "error=reauthentication_required" in denied["Location"]
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    with caplog.at_level("INFO", logger="pyracast.security"):
        response = complete(client, begin(client, process="connect"))
    assert response.status_code == 200
    assert [listed["uid"] for listed in response.json()["data"]] == ["google-identity"]
    account = SocialAccount.objects.get(user=user)
    assert account.extra_data == {"email": oauth_profile["email"]}
    assert "social_account_connected" in caplog.text
    listed = client.get(AUTH + "account/providers").json()["data"]
    assert listed[0]["uid"] == account.uid
    assert listed[0]["provider"]["id"] == "google"
    removal = {"provider": "google", "account": account.uid}
    # The session's own connection keeps its reauthentication for the rest of its window.
    token = client.get("/api/session/").json()["csrf_token"]
    with caplog.at_level("INFO", logger="pyracast.security"):
        removed = client.delete(
            AUTH + "account/providers",
            removal,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
    assert removed.status_code == 200
    assert removed.json()["data"] == []
    assert not SocialAccount.objects.exists()
    assert "social_account_disconnected" in caplog.text


def test_last_enabled_provider_cannot_be_removed(oauth_profile: dict[str, Any]) -> None:
    client = Client(enforce_csrf_checks=True)
    complete(client, begin(client))
    assert (
        submit(
            client,
            "auth/provider/signup",
            {"email": oauth_profile["email"], "company_name": "Solar"},
        ).status_code
        == 200
    )
    user = User.objects.get()
    SocialAccount.objects.create(user=user, provider="disabled", uid="disabled")
    assert (
        submit(client, "auth/reauthenticate/code/request", {"email": user.email}).status_code == 200
    )
    assert (
        submit(client, "auth/reauthenticate/code/confirm", {"code": mailed_code()}).status_code
        == 200
    )
    token = client.get("/api/session/").json()["csrf_token"]
    denied = client.delete(
        AUTH + "account/providers",
        {"provider": "google", "account": "google-identity"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert denied.status_code == 400
    assert SocialAccount.objects.filter(provider="google", user=user).exists()
    assert submit(client, "account/password/change", {"new_password": PASSWORD}).status_code == 401
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


@pytest.mark.parametrize(
    ("staff", "totp"), [(False, False), (False, True), (True, False), (True, True)]
)
def test_existing_social_login_preserves_staff_enrollment_and_local_mfa(
    oauth_profile: dict[str, Any], staff: bool, totp: bool
) -> None:
    user = member()
    user.is_staff = staff
    user.save(update_fields=["is_staff"])
    SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    if totp:
        Authenticator.objects.create(
            user=user, type="totp", data={"secret": generate_totp_secret()}
        )
    client = Client()
    response = complete(client, begin(client))
    if staff and not totp:
        assert response.status_code == 403
        assert client.get("/api/session/").json()["user"] is None
    elif totp:
        flows = client.get(AUTH + "auth/session").json()["data"]["flows"]
        assert any(flow["id"] == "mfa_authenticate" and flow.get("is_pending") for flow in flows)
        assert client.get("/api/session/").json()["user"] is None
    else:
        assert client.get("/api/session/").json()["user"]["id"] == user.pk
    assert SocialAccount.objects.get().extra_data == {"email": oauth_profile["email"]}


def test_callback_state_is_session_bound_single_use_and_errors_are_safe(
    oauth_profile: dict[str, Any],
) -> None:
    client = Client()
    state = begin(client)
    outsider = Client()
    assert "error=" in callback(outsider, state)["Location"]
    assert "error=" in callback(client, state, "microsoft")["Location"]
    assert not SocialAccount.objects.exists()
    assert callback(client, state)["Location"] == CALLBACK
    assert "error=" in callback(client, state)["Location"]
    state = begin(client)
    response = client.get(
        "/api/accounts/google/login/callback/", {"state": state, "error": "access_denied"}
    )
    assert "error=" in response["Location"]
    assert not User.objects.exists()


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/",
        "//evil.example/",
        "https://testserver/admin/",
        "https://testserver/?social_callback=1",
        "https://testserver/app/",
        "https://testserver/app/?social_callback=0",
        "https://testserver/app/?social_callback=1#other",
        CALLBACK + "&next=//evil.example",
    ],
)
def test_unexpected_callback_destinations_return_to_the_configured_callback(url: str) -> None:
    response = Client().post(
        AUTH + "auth/provider/redirect",
        {"provider": "google", "process": "login", "callback_url": url},
    )
    assert response.status_code == 302
    assert response["Location"] == CALLBACK + "&error=invalid_request"


def test_a_provider_callback_without_state_returns_to_the_current_public_origin() -> None:
    with override_settings(PYRACAST_PUBLIC_ORIGIN="https://app.example.com"):
        response = Client().get("/api/accounts/google/login/callback/", {"code": "provider-code"})

    assert response.status_code == 302
    assert response["Location"] == (
        "https://app.example.com/app/?social_callback=1&error=unknown&error_process=login"
    )


def test_an_unknown_provider_returns_to_the_configured_callback() -> None:
    response = Client().post(
        AUTH + "auth/provider/redirect",
        {"provider": "unknown", "process": "login", "callback_url": CALLBACK},
    )
    assert response.status_code == 302
    assert response["Location"] == CALLBACK + "&error=invalid_request"


def test_native_token_and_browser_login_entrypoints_are_unavailable() -> None:
    client = Client()
    for path in (
        AUTH + "auth/provider/token",
        "/api/accounts/google/login/token/",
        "/api/accounts/google/login/",
        "/api/accounts/microsoft/login/",
    ):
        assert client.get(path).status_code == 404
        assert client.post(path, {}).status_code == 404
    assert client.get(AUTH + "account/providers").status_code == 401
    assert client.get(AUTH + "auth/provider/signup").status_code == 409


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_provider_callbacks_reject_unsupported_methods_without_consuming_state(
    oauth_profile: dict[str, Any], provider: str
) -> None:
    client = Client()
    state = begin(client, provider)
    query = urlencode({"state": state, "code": "provider-code"})
    path = f"/api/accounts/{provider}/login/callback/?{query}"
    for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        response = client.generic(method, path)
        assert response.status_code == 405
        assert response["Allow"] == "GET"
    assert not User.objects.exists()
    assert callback(client, state, provider)["Location"] == CALLBACK


@pytest.mark.parametrize(
    "failure", ["user_changed", "logged_out", "reauth_expired", "reauth_missing", "origin_missing"]
)
def test_connect_callback_remains_bound_to_original_recently_reauthenticated_user(
    oauth_profile: dict[str, Any], failure: str
) -> None:
    user = member()
    other = User.objects.create_user(username="other", email="other@example.com", password=PASSWORD)
    existing = SocialAccount.objects.create(
        user=other,
        provider="google",
        uid="google-identity",
        extra_data={"email": "old@gmail.com"},
    )
    client = Client()
    assert (
        submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 200
    )
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    state = begin(client, process="connect")
    if failure in {"user_changed", "logged_out"}:
        states = client.session["socialaccount_states"]
        if failure == "user_changed":
            client.force_login(other)
        else:
            client.logout()
        session = client.session
        session["socialaccount_states"] = states
        session.save()
    elif failure == "origin_missing":
        session = client.session
        states = session["socialaccount_states"]
        states[state][0]["data"] = {}
        session["socialaccount_states"] = states
        session.save()
    else:
        session = client.session
        records = session["account_authentication_methods"]
        for record in records:
            if failure == "reauth_expired":
                record["at"] = time.time() - 600
            else:
                record.pop("reauthenticated", None)
        session["account_authentication_methods"] = records
        session.save()
    response = complete(client, state)
    assert response.status_code == 401
    assert SocialAccount.objects.count() == 1
    existing.refresh_from_db()
    assert existing.user_id == other.pk
    # A refused connection stores nothing, not even the provider's newer profile.
    assert existing.extra_data == {"email": "old@gmail.com"}
    assert "account_reauthentication_state" not in client.session


def test_connect_rejects_identity_owned_by_other_user(oauth_profile: dict[str, Any]) -> None:
    user = member()
    other = User.objects.create_user(username="other")
    SocialAccount.objects.create(
        user=other, provider="google", uid="google-identity", extra_data={"email": "old@gmail.com"}
    )
    client = Client()
    submit(client, "auth/login", {"email": user.email, "password": PASSWORD})
    submit(client, "auth/reauthenticate", {"password": PASSWORD})
    response = complete(client, begin(client, process="connect"))
    assert response.status_code == 400
    assert [error["code"] for error in response.json()["errors"]] == ["connected_other"]
    identity = SocialAccount.objects.get()
    assert (identity.user_id, identity.extra_data) == (other.pk, {"email": "old@gmail.com"})


def test_disabling_provider_expires_pending_signup_and_callback(
    oauth_profile: dict[str, Any],
) -> None:
    client = Client()
    complete(client, begin(client))
    state = begin(client)
    with override_settings(SOCIALACCOUNT_PROVIDERS={}):
        assert client.get(AUTH + "auth/provider/signup").status_code == 409
        assert client.get(AUTH + "auth/session").status_code == 401
        assert "error=provider_unavailable" in callback(client, state)["Location"]
        token = client.get("/api/session/").json()["csrf_token"]
        response = client.post(
            AUTH + "auth/provider/redirect",
            {"provider": "google", "process": "login", "callback_url": CALLBACK},
            HTTP_X_CSRFTOKEN=token,
        )
        assert "error=" in response["Location"]


def test_social_signup_has_same_rate_limit_as_password_signup(
    oauth_profile: dict[str, Any],
) -> None:
    client = Client()
    complete(client, begin(client))
    with override_settings(ACCOUNT_RATE_LIMITS={"signup": "1/h/ip"}):
        assert submit(client, "auth/signup", {}).status_code == 400
        assert submit(client, "auth/provider/signup", {}).status_code == 429
        assert not User.objects.exists()


def test_a_social_signup_may_reuse_an_existing_workspace_name(
    oauth_profile: dict[str, Any],
) -> None:
    existing = Tenant.objects.create(name="Existing Solar")
    client = Client()
    complete(client, begin(client))

    response = submit(
        client,
        "auth/provider/signup",
        {"email": oauth_profile["email"], "company_name": "Existing Solar"},
    )

    assert response.status_code == 200
    membership = Membership.objects.get(user__email=oauth_profile["email"])
    assert membership.tenant_id != existing.pk
    assert (membership.tenant.name, membership.role) == ("Existing Solar", WorkspaceRole.ADMIN)


def test_a_refused_social_signup_rolls_back_identity_and_preserves_pending_signup(
    oauth_profile: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Client()
    complete(client, begin(client))
    save = SignupInput.save

    def refused(self: SignupInput, request: HttpRequest) -> User:
        save(self, request)
        raise ImmediateHttpResponse(HttpResponse(status=400))

    monkeypatch.setattr(SignupInput, "save", refused)
    response = submit(
        client,
        "auth/provider/signup",
        {"email": oauth_profile["email"], "company_name": "Refused Solar"},
    )
    assert response.status_code == 400
    assert not User.objects.exists()
    assert not SocialAccount.objects.exists()
    assert not Membership.objects.exists()
    assert client.get(AUTH + "auth/provider/signup").status_code == 200
    monkeypatch.undo()
    assert (
        submit(
            client,
            "auth/provider/signup",
            {"email": oauth_profile["email"], "company_name": "Fresh Solar"},
        ).status_code
        == 200
    )


def test_unrelated_signup_database_failure_is_not_misreported(
    oauth_profile: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Client()
    complete(client, begin(client))

    def failed_save(self: SignupInput, request: HttpRequest) -> User:
        raise IntegrityError("unrelated database failure")

    monkeypatch.setattr(SignupInput, "save", failed_save)
    with pytest.raises(IntegrityError, match="unrelated database failure"):
        submit(
            client,
            "auth/provider/signup",
            {"email": oauth_profile["email"], "company_name": "Solar"},
        )
    assert not User.objects.exists()


def test_adapter_rejects_disabled_identity_without_retaining_extra_profile_fields() -> None:
    user = member()
    account = SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    request = HttpRequest()
    request.user = user
    adapter = SocialAccountAdapter()
    with context.request_context(request):
        provider = get_adapter().get_provider(request, "google")
        login = provider.sociallogin_from_response(
            request, {"sub": account.uid, "email": user.email, "jobTitle": "Private"}
        )
        login.lookup()
        with (
            override_settings(SOCIALACCOUNT_PROVIDERS={}),
            pytest.raises(PermissionDenied, match="unavailable"),
        ):
            adapter.pre_social_login(request, login)
    account.refresh_from_db()
    assert account.extra_data == {"email": user.email}


def test_disabled_connection_remains_visible_and_removable() -> None:
    user = member()
    account = SocialAccount.objects.create(
        user=user,
        provider="microsoft",
        uid="old-microsoft",
        extra_data={"email": "old@example.com"},
    )
    client = Client(enforce_csrf_checks=True)
    submit(client, "auth/login", {"email": user.email, "password": PASSWORD})
    submit(client, "auth/reauthenticate", {"password": PASSWORD})
    with override_settings(SOCIALACCOUNT_PROVIDERS={"google": PROVIDERS["google"]}):
        linked = client.get(AUTH + "account/providers").json()["data"]
        assert linked[0]["provider"]["id"] == "microsoft"
        assert linked[0]["provider"]["flows"] == []
        assert linked[0]["display"] == "old@example.com"
        token = client.get("/api/session/").json()["csrf_token"]
        removed = client.delete(
            AUTH + "account/providers",
            {"provider": "microsoft", "account": account.uid},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        assert removed.status_code == 200
        assert removed.json()["data"] == []
    assert not SocialAccount.objects.exists()


def test_concurrent_sessions_cannot_disconnect_both_passwordless_login_methods() -> None:
    user = member()
    accounts = [
        SocialAccount.objects.create(user=user, provider=provider, uid=f"{provider}-identity")
        for provider in PROVIDERS
    ]
    # The account has no password; its sessions reauthenticate with a mailed code.
    user.set_unusable_password()
    user.save(update_fields=["password"])
    clients = [Client(enforce_csrf_checks=True), Client(enforce_csrf_checks=True)]
    tokens = []
    for client in clients:
        client.force_login(user)
        request = {"email": user.email}
        assert submit(client, "auth/reauthenticate/code/request", request).status_code == 200
        confirmation = {"code": mailed_code()}
        assert submit(client, "auth/reauthenticate/code/confirm", confirmation).status_code == 200
        tokens.append(client.get("/api/session/").json()["csrf_token"])
    barrier = Barrier(2)

    def disconnect(index: int) -> int:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            account = accounts[index]
            return (
                clients[index]
                .delete(
                    AUTH + "account/providers",
                    {"provider": account.provider, "account": account.uid},
                    content_type="application/json",
                    HTTP_X_CSRFTOKEN=tokens[index],
                )
                .status_code
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(disconnect, index) for index in range(2)]
        statuses = sorted(future.result(timeout=20) for future in futures)
    # The first removal ends the account's other sessions, so the second is refused either as
    # the last way to sign in (400) or because its session has ended (401).
    assert statuses in ([200, 400], [200, 401])
    assert SocialAccount.objects.filter(user=user).count() == 1


@pytest.mark.parametrize("principal", ["owner@example.com", ""])
def test_microsoft_without_mail_uses_principal_or_requests_email(
    oauth_profile: dict[str, Any], principal: str
) -> None:
    oauth_profile.pop("mail")
    oauth_profile["userPrincipalName"] = principal
    client = Client()
    complete(client, begin(client, "microsoft"), "microsoft")
    prefill = client.get(AUTH + "auth/provider/signup").json()["data"]
    assert prefill["email"] == (
        [{"email": principal, "verified": False, "primary": True}] if principal else []
    )
    assert client.session["socialaccount_sociallogin"]["account"]["extra_data"] == {
        "email": principal
    }


@pytest.mark.parametrize("change", ["password", "deactivated", "deleted", "staff", "renamed"])
def test_connect_rechecks_current_user_after_provider_exchange(
    oauth_profile: dict[str, Any], change: str
) -> None:
    user = member()
    client = Client()
    submit(client, "auth/login", {"email": user.email, "password": PASSWORD})
    submit(client, "auth/reauthenticate", {"password": PASSWORD})
    state = begin(client, process="connect")
    profile_ready = Event()
    continue_profile = Event()

    def block_profile() -> None:
        profile_ready.set()
        assert continue_profile.wait(timeout=10)

    oauth_profile["_before_profile"] = block_profile

    def callback() -> Any:
        close_old_connections()
        try:
            return complete(client, state)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(callback)
        assert profile_ready.wait(timeout=10)
        try:
            if change == "password":
                user.set_password("replacement-" + PASSWORD)
                user.save(update_fields=["password"])
            elif change == "deactivated":
                User.objects.filter(pk=user.pk).update(is_active=False)
            elif change == "deleted":
                user.delete()
            elif change == "staff":
                User.objects.filter(pk=user.pk).update(is_staff=True, is_superuser=True)
            else:
                User.objects.filter(pk=user.pk).update(first_name="Renamed")
        finally:
            continue_profile.set()
        response = future.result(timeout=20)
    if change == "renamed":
        # A change outside the credential state keeps the reauthentication.
        assert response.status_code == 200
        assert SocialAccount.objects.get().user_id == user.pk
    else:
        # The staff flags are part of the credential state, so changing them ends the
        # reauthentication the connection rests on; a password change signs the session out,
        # which leaves no login stored for it.
        assert response.status_code == (409 if change == "password" else 401)
        assert not SocialAccount.objects.exists()
        if change == "password":
            user.refresh_from_db()
            assert user.check_password("replacement-" + PASSWORD)
        elif change == "deactivated":
            user.refresh_from_db()
            assert not user.is_active
        elif change == "staff":
            user.refresh_from_db()
            assert user.is_staff
            assert user.is_superuser
            assert user.check_password(PASSWORD)


def test_concurrent_provider_claim_rejects_second_user_without_losing_identity(
    oauth_profile: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    users = [
        member(),
        User.objects.create_user(username="other", email="other@example.com", password=PASSWORD),
    ]
    EmailAddress.objects.create(user=users[1], email=users[1].email, verified=True, primary=True)
    clients = [Client(), Client()]
    states = []
    for client, user in zip(clients, users, strict=True):
        submit(client, "auth/login", {"email": user.email, "password": PASSWORD})
        submit(client, "auth/reauthenticate", {"password": PASSWORD})
        states.append(begin(client, process="connect"))
    barrier = Barrier(2)
    original = SocialLogin.connect

    def synchronized_connect(login: SocialLogin, request: HttpRequest, user: User) -> None:
        barrier.wait(timeout=10)
        original(login, request, user)

    monkeypatch.setattr(SocialLogin, "connect", synchronized_connect)

    def connect(index: int) -> Any:
        close_old_connections()
        try:
            return complete(clients[index], states[index])
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(connect, index) for index in range(2)]
        answers = sorted(
            (future.result(timeout=20) for future in futures),
            key=lambda answer: int(answer.status_code),
        )
    assert [answer.status_code for answer in answers] == [200, 400]
    assert [error["code"] for error in answers[1].json()["errors"]] == ["connected_other"]
    assert SocialAccount.objects.count() == 1
    assert SocialAccount.objects.get().user_id in {user.pk for user in users}


@pytest.mark.parametrize("other_unique_table", [False, True])
def test_unrelated_connect_database_failure_is_not_misreported(
    oauth_profile: dict[str, Any], monkeypatch: pytest.MonkeyPatch, other_unique_table: bool
) -> None:
    user = member()
    client = Client()
    submit(client, "auth/login", {"email": user.email, "password": PASSWORD})
    submit(client, "auth/reauthenticate", {"password": PASSWORD})
    Tenant.objects.create(name="Existing workspace")

    def failed_connect(login: SocialLogin, request: HttpRequest, user: User) -> None:
        if other_unique_table:
            Tenant.objects.create(name="Existing workspace")
        raise IntegrityError("unrelated connection failure")

    monkeypatch.setattr(SocialLogin, "connect", failed_connect)
    with pytest.raises(IntegrityError):
        complete(client, begin(client, process="connect"))
    assert not SocialAccount.objects.exists()


@pytest.mark.parametrize("change", ["password", "deactivated"])
def test_disconnect_rechecks_authentication_after_waiting_for_user_lock(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    user = member()
    account = SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    client = Client(enforce_csrf_checks=True)
    submit(client, "auth/login", {"email": user.email, "password": PASSWORD})
    submit(client, "auth/reauthenticate", {"password": PASSWORD})
    token = client.get("/api/session/").json()["csrf_token"]
    lock_requested = Event()
    original = processes.account_state

    def observed_lock(user_id: Any, *, lock: bool = False) -> str | None:
        if lock:
            lock_requested.set()
        return original(user_id, lock=lock)

    monkeypatch.setattr(processes, "account_state", observed_lock)

    def disconnect() -> int:
        close_old_connections()
        try:
            return client.delete(
                AUTH + "account/providers",
                {"provider": account.provider, "account": account.uid},
                content_type="application/json",
                HTTP_X_CSRFTOKEN=token,
            ).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            User.objects.select_for_update().get(pk=user.pk)
            future = pool.submit(disconnect)
            assert lock_requested.wait(timeout=10)
            if change == "password":
                user.set_password("replacement-" + PASSWORD)
                user.save(update_fields=["password"])
            else:
                User.objects.filter(pk=user.pk).update(is_active=False)
        assert future.result(timeout=20) == 401
    assert SocialAccount.objects.filter(pk=account.pk).exists()


def test_social_provider_credentials_are_loaded_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for provider in PROVIDERS:
        monkeypatch.setenv(f"PYRACAST_{provider.upper()}_CLIENT_ID", f"{provider}-client")
        monkeypatch.setenv(f"PYRACAST_{provider.upper()}_CLIENT_SECRET", "test-client-secret")
    values = runpy.run_path(str(Path(__file__).resolve().parents[1] / "src/pyracast/settings.py"))
    providers = values["SOCIALACCOUNT_PROVIDERS"]
    assert set(providers) == {"google", "microsoft"}
    for provider, configuration in providers.items():
        assert configuration["APP"]["client_id"] == f"{provider}-client"
        assert configuration["APP"]["secret"] == "test-client-secret"  # noqa: S105
        assert configuration["OAUTH_PKCE_ENABLED"] is True
        assert configuration["VERIFIED_EMAIL"] is False


@pytest.mark.parametrize("provider", ["google", "microsoft"])
@pytest.mark.parametrize("credential", ["CLIENT_ID", "CLIENT_SECRET"])
def test_partial_social_provider_credentials_fail_startup(
    monkeypatch: pytest.MonkeyPatch, provider: str, credential: str
) -> None:
    for candidate in PROVIDERS:
        monkeypatch.setenv(f"PYRACAST_{candidate.upper()}_CLIENT_ID", "")
        monkeypatch.setenv(f"PYRACAST_{candidate.upper()}_CLIENT_SECRET", "")
    monkeypatch.setenv(f"PYRACAST_{provider.upper()}_{credential}", "test-credential")
    with pytest.raises(ImproperlyConfigured, match=provider):
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "src/pyracast/settings.py"))


def test_social_signup_with_invitation_requires_explicit_acceptance(
    oauth_profile: dict[str, Any],
) -> None:
    owner = member()
    tenant = Tenant.objects.create(name="Inviting workspace")
    Membership.objects.create(user=owner, tenant=tenant, role=WorkspaceRole.ADMIN)
    invite_workspace_member(owner, tenant.pk, oauth_profile["email"])
    invitation = invitation_token()
    client = Client(enforce_csrf_checks=True)
    complete(client, begin(client))
    invalid = submit(
        client,
        "auth/provider/signup",
        {"email": oauth_profile["email"], "invitation_token": "invalid-token"},
    )
    assert invalid.status_code == 400
    assert User.objects.count() == 1
    response = submit(
        client,
        "auth/provider/signup",
        {"email": oauth_profile["email"], "invitation_token": invitation},
    )
    assert response.status_code == 200
    invited = User.objects.get(email=oauth_profile["email"])
    assert not Membership.objects.filter(user=invited).exists()
    assert Tenant.objects.count() == 1
    token = client.get("/api/session/").json()["csrf_token"]
    accepted = client.post(
        "/api/invitations/accept/",
        {"token": invitation},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"tenant_id": tenant.pk, "role": "viewer"}
    assert Membership.objects.get(user=invited).tenant_id == tenant.pk
    assert Tenant.objects.count() == 1


def test_provider_network_failure_returns_safe_frontend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failure(
        self: requests.Session, method: str, url: str, *args: Any, **kwargs: Any
    ) -> requests.Response:
        raise requests.Timeout("confidential provider diagnostic")

    monkeypatch.setattr(requests.Session, "request", failure)
    client = Client()
    response = callback(client, begin(client))
    assert response.status_code == 302
    assert "error=" in response["Location"]
    assert "confidential" not in response["Location"]
    assert not User.objects.exists()
