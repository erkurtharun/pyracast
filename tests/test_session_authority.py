import re
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from allauth.account.internal.flows.login import get_authentication_records
from allauth.account.models import EmailAddress
from allauth.core.exceptions import ReauthenticationRequired
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import transaction
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from pyracast.accounts import inputs, processes
from pyracast.accounts.processes import lock_account
from pyracast.accounts.session_cookie import persist
from tests.support.account_sessions import (
    alongside,
    browser,
    capture_security_events,
    elsewhere,
    passwordless,
    session_key,
    session_user,
    totp_clock,
    with_totp,
)
from tests.support.api import verified_admin
from tests.support.auth_lifecycle import PASSWORD, mailed_code, member, submit, totp_code
from tests.support.company_registration import signup
from tests.support.social_accounts import (
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
    assert complete(client, begin(client)).status_code == 200
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


def password_confirmed(client: Client, _user: User, _secret: str, _now: float) -> Any:
    return submit(client, "auth/reauthenticate", {"password": PASSWORD})


def code_confirmed(client: Client, user: User, _secret: str, _now: float) -> Any:
    requested = submit(client, "auth/reauthenticate/code/request", {"email": user.email})
    assert requested.status_code == 200
    return submit(client, "auth/reauthenticate/code/confirm", {"code": mailed_code()})


def authenticator_confirmed(client: Client, _user: User, secret: str, now: float) -> Any:
    return submit(client, "auth/2fa/reauthenticate", {"code": totp_code(secret, now)})


def console_confirmed(client: Client, _user: User, secret: str, now: float) -> Any:
    return client.post(reverse("admin:workspace_confirm"), {"code": totp_code(secret, now)})


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


CONFIRMATIONS: dict[str, tuple[Callable[[Client, User, str, float], Any], int]] = {
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

    assert confirm(client, user, secret, now).status_code == answered

    assert holds_current_authority(client, user.pk)


FACTOR_CHECKS = {
    "password": (inputs.ReauthenticateInput, "clean_password"),
    "authenticator": (inputs.SecondFactorCode, "matching_code"),
    "console": (inputs.SecondFactorCode, "matching_code"),
}


def after_the_factor_check(
    monkeypatch: pytest.MonkeyPatch, kind: str, work: Callable[[], object]
) -> list[Any]:
    # Runs `work` from another connection once the confirmation checked its factor, and notes
    # whether it had finished when the confirmation went on.
    owner, name = FACTOR_CHECKS[kind]
    check = getattr(owner, name)
    started: list[Any] = []

    def checked(self: Any, *args: Any, **kwargs: Any) -> Any:
        value = check(self, *args, **kwargs)
        if not started:
            future = alongside(work)
            started.append((future.done(), future))
        return value

    monkeypatch.setattr(owner, name, checked)
    return started


def connected_elsewhere(user: User) -> Callable[[], None]:
    # Another session connects a provider identity, which locks the account first.
    def connect() -> None:
        with transaction.atomic():
            lock_account(user.pk)
            SocialAccount.objects.create(user=user, provider="google", uid="connected-elsewhere")

    return connect


@pytest.mark.parametrize("kind", ["password", "authenticator", "console"])
def test_a_connection_made_while_a_confirmation_checks_its_factor_commits_after_its_record(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    totp_clock(monkeypatch, now)
    client, user, secret = confirming(kind)
    started = after_the_factor_check(monkeypatch, kind, connected_elsewhere(user))
    confirm, answered = CONFIRMATIONS[kind]

    assert confirm(client, user, secret, now).status_code == answered
    [(finished, connection)] = started
    connection.result(timeout=10)

    assert not finished
    assert SocialAccount.objects.filter(user=user, uid="connected-elsewhere").exists()
    # The record names the state the factor was checked under, which the connection then changed.
    assert not holds_current_authority(client, user.pk)


def test_a_confirmation_whose_session_ended_while_its_code_was_checked_spends_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    user = member()
    with_totp(user)
    [code, *_rest] = RecoveryCodes.activate(user).get_unused_codes()
    client = signed_in(user)
    key = session_key(client)
    owner, name = FACTOR_CHECKS["authenticator"]
    check = getattr(owner, name)

    def checked_then_ended(self: Any, *args: Any, **kwargs: Any) -> Any:
        value = check(self, *args, **kwargs)
        elsewhere(lambda: Session.objects.filter(session_key=key).delete())
        return value

    monkeypatch.setattr(owner, name, checked_then_ended)
    events = capture_security_events(caplog, monkeypatch)
    client.raise_request_exception = False

    response = submit(client, "auth/2fa/reauthenticate", {"code": code})

    assert (response.status_code, response.json()["code"]) == (400, "session_ended")
    codes = Authenticator.objects.get(user=user, type=Authenticator.Type.RECOVERY_CODES).wrap()
    assert code in codes.get_unused_codes()
    assert {"reauthenticated", "mfa_succeeded"} & {line["event"] for line in events()} == set()


def before_the_confirmation_locks(
    monkeypatch: pytest.MonkeyPatch, work: Callable[[], object]
) -> None:
    lock = processes.lock_account

    def changed_then_locked(user_id: Any) -> None:
        elsewhere(work)
        lock(user_id)

    monkeypatch.setattr(processes, "lock_account", changed_then_locked)


def refusal(response: Any) -> tuple[int, str | None]:
    if response["Content-Type"] == "application/json":
        return response.status_code, response.json().get("code")
    title = re.search(r"<title>(.*)</title>", response.content.decode())
    return response.status_code, title and title[1]


ENDED = {
    "password": (400, "session_ended"),
    "authenticator": (400, "session_ended"),
    "console": (400, "Bad Request (400)"),
}


@pytest.mark.parametrize("kind", ENDED)
def test_a_confirmation_whose_session_ended_before_it_took_the_locks_is_refused_and_records_nothing(
    kind: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now = time.time()
    totp_clock(monkeypatch, now)
    client, user, secret = confirming(kind)
    key = session_key(client)
    before_the_confirmation_locks(
        monkeypatch, lambda: Session.objects.filter(session_key=key).delete()
    )
    events = capture_security_events(caplog, monkeypatch)
    client.raise_request_exception = False
    confirm, _answered = CONFIRMATIONS[kind]

    response = confirm(client, user, secret, now)

    assert refusal(response) == ENDED[kind]
    assert session_user(client) is None
    assert {"reauthenticated", "mfa_succeeded"} & {line["event"] for line in events()} == set()


def test_a_console_confirmation_whose_authenticator_was_removed_before_its_locks_refuses_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = time.time()
    totp_clock(monkeypatch, now)
    client, user, secret = confirming("console")
    before_the_confirmation_locks(
        monkeypatch, lambda: Authenticator.objects.filter(user=user).delete()
    )

    response = console_confirmed(client, user, secret, now)

    assert response.status_code == 400
    assert response.context["form"].has_error("code", "incorrect_code")
    assert not holds_current_authority(client, user.pk)


ADDED = "added@example.com"


def test_a_session_without_an_authority_record_confirms_before_it_completes_an_address_change() -> (
    None
):
    # The change was started in a session from before sessions carried an authority record.
    user = member()
    client = signed_in(user)
    with_proof(client, user, "password", age=0, reauthenticated=True)
    assert submit(client, "account/email", {"email": ADDED}).status_code == 200
    forget_authority(client)
    code = {"key": mailed_code()}

    refused = submit(client, "auth/email/verify", code)

    assert refused.status_code == 401
    assert [flow["id"] for flow in refused.json()["data"]["flows"]] == ["reauthenticate"]
    assert not EmailAddress.objects.filter(user=user, email=ADDED, verified=True).exists()
    assert session_user(client) == user.pk
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    assert submit(client, "auth/email/verify", code).status_code == 200
    assert EmailAddress.objects.filter(user=user, email=ADDED, verified=True).exists()


def test_a_signed_in_session_completes_a_sign_up_code_of_another_account_by_that_code() -> None:
    # The code was mailed to a sign-up this browser started before it signed in to another
    # account; the session's own authority record plays no part in it.
    client = browser()
    assert signup(client).status_code == 401
    code = {"key": mailed_code()}
    user = member()
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 200
    EmailAddress.objects.create(user=user, email=SECOND, verified=True)

    response = submit(client, "auth/email/verify", code)

    assert response.status_code == 200
    assert EmailAddress.objects.get(email="owner@example.com").verified
    assert session_user(client) == user.pk


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


def test_a_credential_write_of_a_request_without_an_account_asks_for_a_confirmation() -> None:
    request = RequestFactory().post("/")
    request.user = AnonymousUser()

    with pytest.raises(ReauthenticationRequired):
        processes.authorize_credential_write(request)


def test_an_address_request_whose_session_ended_before_it_was_saved_is_answered_as_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = member()
    client = signed_in(user)
    with_proof(client, user, "password", age=0, reauthenticated=True)
    key = session_key(client)
    stamp = processes.stamp_started

    def ended_then_stamped(*args: Any, **kwargs: Any) -> None:
        elsewhere(lambda: Session.objects.filter(session_key=key).delete())
        stamp(*args, **kwargs)

    monkeypatch.setattr(processes, "stamp_started", ended_then_stamped)
    client.raise_request_exception = False

    response = submit(client, "account/email", {"email": ADDED})

    assert (response.status_code, response.json()["code"]) == (400, "session_ended")
    assert not EmailAddress.objects.filter(email=ADDED).exists()


def test_a_session_without_a_key_is_not_saved() -> None:
    request = RequestFactory().post("/")
    request.session = SessionStore()

    persist(request)

    assert request.session.session_key is None


def test_a_state_of_a_deleted_account_ends_the_confirmations_the_session_holds() -> None:
    user = member()
    client = signed_in(user)
    with_proof(client, user, "password", age=0, reauthenticated=True)
    User.objects.filter(pk=user.pk).delete()
    request = RequestFactory().post("/")
    request.session = client.session

    with transaction.atomic():
        processes.record_authority(request, user)

    assert [record["reauthenticated"] for record in get_authentication_records(request, user)] == [
        False
    ]
