import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse

from tests.support.account_sessions import (
    browser,
    passwordless,
    session_user,
    totp_clock,
    with_totp,
)
from tests.support.api import verified_admin
from tests.support.auth_lifecycle import PASSWORD, mailed_code, member, submit, totp_code
from tests.support.company_registration import signup
from tests.support.social_accounts import (
    CALLBACK,
    PROVIDERS,
    begin,
    complete,
    serve_provider_profile,
)
from tests.support.step_up import forget_authority, holds_current_authority, signed_in, with_proof

pytestmark = pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)

CHANGED_PASSWORD = PASSWORD + "-changed"
SECOND = "second@example.com"


@pytest.fixture(autouse=True)
def local_settings() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
        SOCIALACCOUNT_PROVIDERS=PROVIDERS,
    ):
        yield


SignIn = Callable[[pytest.MonkeyPatch, float], tuple[Client, User]]
LOGIN = "auth/login"


def by_password(_monkeypatch: pytest.MonkeyPatch, _now: float) -> tuple[Client, User]:
    user = member()
    client = browser()
    assert submit(client, LOGIN, {"email": user.email, "password": PASSWORD}).status_code == 200
    return client, user


def by_mailed_code(_monkeypatch: pytest.MonkeyPatch, _now: float) -> tuple[Client, User]:
    user = member()
    EmailAddress.objects.filter(user=user).update(verified=False)
    client = browser()
    assert submit(client, LOGIN, {"email": user.email, "password": PASSWORD}).status_code == 401
    assert submit(client, "auth/email/verify", {"key": mailed_code()}).status_code == 200
    return client, user


def by_second_factor(monkeypatch: pytest.MonkeyPatch, now: float) -> tuple[Client, User]:
    user = member()
    secret = with_totp(user)
    totp_clock(monkeypatch, now)
    client = browser()
    assert submit(client, LOGIN, {"email": user.email, "password": PASSWORD}).status_code == 401
    code = {"code": totp_code(secret, now)}
    assert submit(client, "auth/2fa/authenticate", code).status_code == 200
    return client, user


def by_provider(monkeypatch: pytest.MonkeyPatch, _now: float) -> tuple[Client, User]:
    serve_provider_profile(monkeypatch)
    user = member()
    SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    client = browser()
    assert complete(client, begin(client))["Location"] == CALLBACK
    return client, user


def by_provider_signup(monkeypatch: pytest.MonkeyPatch, _now: float) -> tuple[Client, User]:
    profile = serve_provider_profile(monkeypatch)
    client = browser()
    complete(client, begin(client))
    details = {"email": profile["email"], "company_name": "Solar"}
    assert submit(client, "auth/provider/signup", details).status_code == 200
    return client, User.objects.get()


def by_signup_verification(_monkeypatch: pytest.MonkeyPatch, _now: float) -> tuple[Client, User]:
    client = browser()
    assert signup(client).status_code == 401
    assert submit(client, "auth/email/verify", {"key": mailed_code()}).status_code == 200
    return client, User.objects.get()


SIGN_INS: dict[str, SignIn] = {
    "password": by_password,
    "mailed-code": by_mailed_code,
    "second-factor": by_second_factor,
    "provider": by_provider,
    "provider-signup": by_provider_signup,
    "signup-verification": by_signup_verification,
}


@pytest.mark.parametrize("path", SIGN_INS)
def test_every_sign_in_records_the_state_the_session_signed_in_under(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, user = SIGN_INS[path](monkeypatch, time.time())

    assert session_user(client) == user.pk
    assert holds_current_authority(client, user.pk)


def password_confirmed(client: Client, _user: User, _secret: str, _now: float) -> int:
    return int(submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code)


def code_confirmed(client: Client, user: User, _secret: str, _now: float) -> int:
    requested = submit(client, "auth/reauthenticate/code/request", {"email": user.email})
    assert requested.status_code == 200
    return int(
        submit(client, "auth/reauthenticate/code/confirm", {"code": mailed_code()}).status_code
    )


def authenticator_confirmed(client: Client, _user: User, secret: str, now: float) -> int:
    code = {"code": totp_code(secret, now)}
    return int(submit(client, "auth/2fa/reauthenticate", code).status_code)


def console_confirmed(client: Client, _user: User, secret: str, now: float) -> int:
    confirm = reverse("admin:workspace_confirm")
    return int(client.post(confirm, {"code": totp_code(secret, now)}).status_code)


def confirming(kind: str) -> tuple[Client, User, str]:
    if kind == "console":
        client, user = verified_admin(confirmed=False)
        Authenticator.objects.filter(user=user).delete()
        secret = generate_totp_secret()
        factor = TOTP.activate(user, secret).instance
        with_proof(
            client, user, "mfa", age=60, reauthenticated=False, type=factor.type, id=factor.pk
        )
        return client, user, secret
    user = passwordless("holder") if kind == "code" else member()
    secret = with_totp(user) if kind == "authenticator" else ""
    return signed_in(user), user, secret


CONFIRMATIONS: dict[str, tuple[Callable[[Client, User, str, float], int], int]] = {
    "password": (password_confirmed, 200),
    "code": (code_confirmed, 200),
    "authenticator": (authenticator_confirmed, 200),
    "console": (console_confirmed, 302),
}


@pytest.mark.parametrize("kind", CONFIRMATIONS)
def test_every_confirmation_records_the_state_it_was_given_under(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    totp_clock(monkeypatch, now)
    client, user, secret = confirming(kind)
    EmailAddress.objects.create(user=user, email=SECOND, verified=True)
    assert not holds_current_authority(client, user.pk)
    confirm, answered = CONFIRMATIONS[kind]

    assert confirm(client, user, secret, now) == answered

    assert holds_current_authority(client, user.pk)


def change_password(client: Client) -> Any:
    data = {"current_password": PASSWORD, "new_password": CHANGED_PASSWORD}
    return submit(client, "account/password/change", data)


def assert_confirmation_asked_first(client: Client, user: User) -> None:
    refused = change_password(client)

    assert refused.status_code == 401
    assert [flow["id"] for flow in refused.json()["data"]["flows"]] == ["reauthenticate"]
    assert User.objects.get(pk=user.pk).check_password(PASSWORD)
    assert session_user(client) == user.pk
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    changed = change_password(client)
    assert changed.json()["meta"]["password_changed"] is True
    assert User.objects.get(pk=user.pk).check_password(CHANGED_PASSWORD)


def test_a_session_signed_in_under_the_current_state_changes_its_password_by_the_current_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, user = by_password(monkeypatch, time.time())

    changed = change_password(client)

    assert changed.json()["meta"]["password_changed"] is True
    assert User.objects.get(pk=user.pk).check_password(CHANGED_PASSWORD)


def test_a_session_without_an_authority_record_confirms_before_it_changes_its_password() -> None:
    user = member()
    client = signed_in(user)
    forget_authority(client)

    assert_confirmation_asked_first(client, user)


def test_after_another_session_changed_the_account_a_password_change_needs_a_confirmation() -> None:
    user = member()
    EmailAddress.objects.create(user=user, email=SECOND, verified=True)
    client, other = signed_in(user), signed_in(user)
    with_proof(other, user, "password", age=0, reauthenticated=True)
    token = other.get("/api/session/").json()["csrf_token"]
    removed = other.delete(
        "/api/auth/browser/v1/account/email",
        {"email": SECOND},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert removed.status_code == 200

    assert_confirmation_asked_first(client, user)
