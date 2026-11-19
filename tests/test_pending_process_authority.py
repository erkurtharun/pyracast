import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from threading import Event
from typing import Any

import pytest
from allauth.account import utils as account_utils
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.account.internal.flows.password_reset_by_code import (
    PASSWORD_RESET_VERIFICATION_SESSION_KEY,
    PasswordResetVerificationProcess,
)
from allauth.account.internal.flows.reauthenticate_by_code import (
    REAUTHENTICATION_CODE_SESSION_KEY,
    ConfirmReauthenticationCodeProcess,
)
from allauth.account.models import EmailAddress
from allauth.core.internal import ratelimit
from allauth.headless.account.inputs import (
    AddEmailInput,
    ConfirmReauthenticationCodeInput,
    RequestReauthenticationCodeInput,
    VerifyEmailInput,
)
from allauth.headless.socialaccount.inputs import SignupInput
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.stages import AuthenticateStage
from allauth.mfa.utils import encrypt
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.db import connections, transaction
from django.http import HttpRequest
from django.test import Client, RequestFactory, override_settings
from django.utils import timezone

from pyracast.accounts import authentication, headless, processes, security, social
from pyracast.accounts.adapters import AccountAdapter
from pyracast.accounts.inputs import (
    LoginInput,
    RequestPasswordResetInput,
    ResetPasswordInput,
    SecondFactorInput,
)
from tests.support.account_sessions import (
    account,
    alongside,
    answer,
    browser,
    elsewhere,
    moment,
    passwordless,
    session_user,
    signed_in,
    totp_clock,
    with_totp,
)
from tests.support.admin_mfa_bootstrap import TEST_PASSWORD, administrator
from tests.support.auth_lifecycle import AUTH, PASSWORD, mailed_code, submit, totp_code
from tests.support.company_registration import signup
from tests.support.social_accounts import (
    PROVIDERS,
    begin,
    complete,
    serve_provider_profile,
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
OWNER = "owner@example.com"
SECOND = "second@example.com"
CHANGED = "changed@example.com"


@pytest.fixture(autouse=True)
def local_mail_and_providers() -> Iterator[None]:
    with override_settings(
        SOCIALACCOUNT_PROVIDERS=PROVIDERS,
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
    ):
        yield


@dataclass
class Pending:
    client: Client
    endpoint: str
    data: dict[str, str]
    user: User
    signed_in: bool = False

    def nothing_pending(self) -> tuple[int, Any]:
        client = signed_in(self.user) if self.signed_in else browser()
        mail.outbox.clear()
        return answer(submit(client, self.endpoint, self.data))


def reset_code(monkeypatch: pytest.MonkeyPatch) -> Pending:
    user = account("owner")
    client = browser()
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    data = {"key": mailed_code(), "password": PASSWORD + "reset"}
    return Pending(client, "auth/password/reset", data, user)


def signup_code(monkeypatch: pytest.MonkeyPatch) -> Pending:
    client = browser()
    assert signup(client).status_code == 401
    user = User.objects.get(email__iexact=OWNER)
    return Pending(client, "auth/email/verify", {"key": mailed_code()}, user)


def signup_resend(monkeypatch: pytest.MonkeyPatch) -> Pending:
    pending = signup_code(monkeypatch)
    monkeypatch.setattr(ratelimit, "time", moment(time.time() + 61))
    return Pending(pending.client, "auth/email/verify/resend", {}, pending.user)


def reauthentication_code(monkeypatch: pytest.MonkeyPatch) -> Pending:
    user = passwordless("owner")
    EmailAddress.objects.create(user=user, email=SECOND, verified=True)
    client = signed_in(user)
    request = {"email": SECOND}
    assert submit(client, "auth/reauthenticate/code/request", request).status_code == 200
    data = {"code": mailed_code()}
    return Pending(client, "auth/reauthenticate/code/confirm", data, user, signed_in=True)


def second_factor(monkeypatch: pytest.MonkeyPatch) -> Pending:
    now = time.time()
    totp_clock(monkeypatch, now)
    user = account("owner")
    secret = with_totp(user)
    client = browser()
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 401
    return Pending(client, "auth/2fa/authenticate", {"code": totp_code(secret, now)}, user)


def provider_second_factor(monkeypatch: pytest.MonkeyPatch) -> Pending:
    now = time.time()
    totp_clock(monkeypatch, now)
    serve_provider_profile(monkeypatch)
    user = account("owner")
    secret = with_totp(user)
    SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    client = browser()
    complete(client, begin(client))
    return Pending(client, "auth/2fa/authenticate", {"code": totp_code(secret, now)}, user)


def password_changed(user: User) -> None:
    user.set_password(PASSWORD + "changed")
    user.save(update_fields=["password"])


def deactivated(user: User) -> None:
    User.objects.filter(pk=user.pk).update(is_active=False)


def deleted(user: User) -> None:
    User.objects.filter(pk=user.pk).delete()


def removes(address: str) -> Callable[[User], None]:
    def remove(user: User) -> None:
        EmailAddress.objects.filter(user=user, email=address).delete()

    return remove


def recreates(address: str) -> Callable[[User], None]:
    def recreate(user: User) -> None:
        row = EmailAddress.objects.get(user=user, email=address)
        row.delete()
        EmailAddress.objects.create(
            user=user, email=address, primary=row.primary, verified=row.verified
        )

    return recreate


def reassigned(user: User) -> None:
    EmailAddress.objects.filter(user=user, email=OWNER).update(user=account("other"), primary=False)


def replaced(user: User) -> None:
    EmailAddress.objects.filter(user=user).delete()
    EmailAddress.objects.create(user=user, email=CHANGED, primary=True, verified=True)


CASES: dict[str, tuple[Callable[[pytest.MonkeyPatch], Pending], Callable[[User], None]]] = {
    "reset-code/password-changed": (reset_code, password_changed),
    "reset-code/deactivated": (reset_code, deactivated),
    "reset-code/address-removed": (reset_code, removes(OWNER)),
    "reset-code/address-recreated": (reset_code, recreates(OWNER)),
    "reset-code/address-reassigned": (reset_code, reassigned),
    "verification-code/deactivated": (signup_code, deactivated),
    "verification-code/address-removed": (signup_code, removes(OWNER)),
    "verification-code/address-recreated": (signup_code, recreates(OWNER)),
    "verification-resend/deactivated": (signup_resend, deactivated),
    "reauthentication-code/address-removed": (reauthentication_code, removes(SECOND)),
    "reauthentication-code/address-recreated": (reauthentication_code, recreates(SECOND)),
    "second-factor/password-changed": (second_factor, password_changed),
    "second-factor/address-removed": (second_factor, removes(OWNER)),
    "second-factor/address-recreated": (second_factor, recreates(OWNER)),
    "second-factor/deactivated": (second_factor, deactivated),
    "second-factor/deleted": (second_factor, deleted),
    "provider-second-factor/address-replaced": (provider_second_factor, replaced),
}


def stored(user: User) -> tuple[list[Any], list[Any]]:
    accounts = User.objects.filter(pk=user.pk).values_list("password", "is_active", "last_login")
    addresses = EmailAddress.objects.order_by("email").values_list(
        "user_id", "email", "primary", "verified"
    )
    return list(accounts), list(addresses)


@pytest.mark.parametrize("case", CASES)
def test_a_pending_process_dies_when_its_authority_changes(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, change = CASES[case]
    pending = start(monkeypatch)
    change(pending.user)
    expected = pending.nothing_pending()
    before = stored(pending.user)

    response = submit(pending.client, pending.endpoint, pending.data)

    assert answer(response) == expected
    assert mail.outbox == []
    assert stored(pending.user) == before
    assert answer(submit(pending.client, pending.endpoint, pending.data)) == expected
    if not pending.signed_in:
        assert session_user(pending.client) is None


def test_a_reset_code_dies_when_the_password_is_changed_in_another_session() -> None:
    user = account("owner")
    reset = browser()
    assert submit(reset, "auth/password/request", {"email": user.email}).status_code == 401
    data = {"key": mailed_code(), "password": PASSWORD + "reset"}
    change = {"current_password": PASSWORD, "new_password": PASSWORD + "changed"}
    assert submit(signed_in(user), "account/password/change", change).status_code == 401
    expected = answer(submit(browser(), "auth/password/reset", data))
    mail.outbox.clear()

    response = submit(reset, "auth/password/reset", data)

    assert answer(response) == expected
    assert mail.outbox == []
    user.refresh_from_db()
    assert user.check_password(PASSWORD + "changed")


def test_a_reset_answers_like_nothing_pending_when_the_password_changes_during_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = reset_code(monkeypatch)
    expected = pending.nothing_pending()
    clean: Callable[..., Any] = ResetPasswordInput.clean

    def changed_meanwhile(self: ResetPasswordInput) -> Any:
        elsewhere(lambda: password_changed(User.objects.get(pk=pending.user.pk)))
        return clean(self)

    monkeypatch.setattr(ResetPasswordInput, "clean", changed_meanwhile)

    response = submit(pending.client, pending.endpoint, pending.data)

    assert answer(response) == expected
    assert mail.outbox == []
    pending.user.refresh_from_db()
    assert pending.user.check_password(PASSWORD + "changed")


def test_a_verification_answers_like_nothing_pending_when_the_account_is_deactivated_during_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = signup_code(monkeypatch)
    expected = pending.nothing_pending()
    clean_key: Callable[..., Any] = VerifyEmailInput.clean_key

    def deactivated_meanwhile(self: VerifyEmailInput) -> Any:
        key = clean_key(self)
        elsewhere(lambda: deactivated(pending.user))
        return key

    monkeypatch.setattr(VerifyEmailInput, "clean_key", deactivated_meanwhile)

    response = submit(pending.client, pending.endpoint, pending.data)

    assert answer(response) == expected
    assert mail.outbox == []
    assert not EmailAddress.objects.get(email=OWNER).verified


Start = Callable[[pytest.MonkeyPatch], Pending]
DURING: dict[str, tuple[Start, type[Any], Callable[[User], None]]] = {
    "reauthentication-code/address-removed": (
        reauthentication_code,
        ConfirmReauthenticationCodeInput,
        removes(SECOND),
    ),
}


@pytest.mark.parametrize("case", DURING)
def test_a_completion_answers_like_nothing_pending_when_its_authority_changes_during_it(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, form, change = DURING[case]
    pending = start(monkeypatch)
    expected = pending.nothing_pending()
    clean: Callable[..., Any] = form.clean

    def changed_meanwhile(self: Any) -> Any:
        cleaned = clean(self)
        elsewhere(lambda: change(User.objects.get(pk=pending.user.pk)))
        return cleaned

    monkeypatch.setattr(form, "clean", changed_meanwhile)

    response = submit(pending.client, pending.endpoint, pending.data)

    assert answer(response) == expected
    assert mail.outbox == []
    assert (session_user(pending.client) == pending.user.pk) is pending.signed_in


Changes = list[tuple[bool, Future[None]]]


def changing(
    monkeypatch: pytest.MonkeyPatch,
    owner: object,
    name: str,
    change: Callable[[], object],
    *,
    after: bool = False,
) -> Changes:
    # Starts the change from another connection once, before or after the patched step, and
    # notes whether it had committed when the request went on.
    changes: Changes = []
    step: Callable[..., Any] = getattr(owner, name)

    def start_change() -> None:
        if not changes:
            future = alongside(change)
            changes.append((future.done(), future))

    def changed_meanwhile(*args: Any, **kwargs: Any) -> Any:
        if not after:
            start_change()
        result = step(*args, **kwargs)
        if after:
            start_change()
        return result

    monkeypatch.setattr(owner, name, changed_meanwhile)
    return changes


def committed_first(changes: Changes) -> bool:
    [(committed, future)] = changes
    future.result(timeout=10)
    return committed


CODE_CHECK_CHANGES: dict[str, tuple[Callable[[User], None], Callable[[User], bool]]] = {
    "password-changed": (
        password_changed,
        lambda user: User.objects.get(pk=user.pk).check_password(PASSWORD + "changed"),
    ),
    "deleted": (deleted, lambda user: not User.objects.filter(pk=user.pk).exists()),
}


@pytest.mark.parametrize("case", CODE_CHECK_CHANGES)
def test_a_change_started_while_a_second_factor_is_checked_waits_and_ends_the_sign_in(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    change, applied = CODE_CHECK_CHANGES[case]
    pending = second_factor(monkeypatch)
    changes = changing(
        monkeypatch,
        SecondFactorInput,
        "clean",
        lambda: change(User.objects.get(pk=pending.user.pk)),
        after=True,
    )

    response = submit(pending.client, pending.endpoint, pending.data)
    waited = not committed_first(changes)

    assert response.status_code == 200
    assert applied(pending.user)
    assert mail.outbox == []
    assert session_user(pending.client) is None
    assert waited


SECOND_FACTOR_STARTS: dict[str, tuple[type[Any], str]] = {
    "after-the-first-factor": (LoginInput, "clean"),
    "while-the-stage-starts": (AuthenticateStage, "handle"),
}


@pytest.mark.parametrize("step", SECOND_FACTOR_STARTS)
def test_a_second_factor_stage_started_while_the_password_changes_never_completes(
    step: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    totp_clock(monkeypatch, now)
    user = account("owner")
    secret = with_totp(user)
    owner, name = SECOND_FACTOR_STARTS[step]
    changes = changing(
        monkeypatch,
        owner,
        name,
        lambda: password_changed(User.objects.get(pk=user.pk)),
        after=True,
    )
    client = browser()
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 401
    committed_first(changes)
    code = {"code": totp_code(secret, now)}
    expected = answer(submit(browser(), "auth/2fa/authenticate", code))

    response = submit(client, "auth/2fa/authenticate", code)

    assert answer(response) == expected
    assert session_user(client) is None


def change_address(client: Client) -> None:
    assert submit(client, "account/email", {"email": CHANGED}).status_code == 200
    assert submit(client, "auth/email/verify", {"key": mailed_code()}).status_code == 200


def test_an_address_replacement_started_after_a_first_factor_waits_and_ends_the_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = account("owner")
    other = signed_in(user, reauthenticated=True)
    changes = changing(monkeypatch, LoginInput, "clean", lambda: change_address(other), after=True)
    client = browser()

    submit(client, "auth/login", {"email": OWNER, "password": PASSWORD})
    waited = not committed_first(changes)
    submit(client, "auth/email/verify", {"key": mailed_code()})

    assert list(EmailAddress.objects.values_list("email", flat=True)) == [CHANGED]
    assert session_user(other) == user.pk
    assert session_user(client) is None
    assert waited


def remove_identity(client: Client) -> None:
    token = client.get("/api/session/").json()["csrf_token"]
    removed = client.delete(
        AUTH + "account/providers",
        {"provider": "google", "account": "google-identity"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert removed.status_code == 200


def test_an_identity_removal_started_during_a_provider_sign_in_waits_and_ends_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_provider_profile(monkeypatch)
    user = account("owner")
    SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    other = signed_in(user, reauthenticated=True)
    client = browser()
    state = begin(client)
    changes = changing(monkeypatch, SocialAccount, "save", lambda: remove_identity(other))

    complete(client, state)
    waited = not committed_first(changes)

    assert not SocialAccount.objects.exists()
    assert session_user(other) == user.pk
    assert session_user(client) is None
    assert waited


RESET_START_CHANGES: dict[str, tuple[Callable[[User], None], str, list[str]]] = {
    "password-changed": (password_changed, PASSWORD + "changed", [OWNER]),
    "address-removed": (removes(OWNER), PASSWORD, []),
}


@pytest.mark.parametrize("case", RESET_START_CHANGES)
def test_a_reset_code_dies_when_its_authority_changes_after_the_code_is_sent(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    change, password, addresses = RESET_START_CHANGES[case]
    user = account("owner")
    changes = changing(
        monkeypatch,
        PasswordResetVerificationProcess,
        "send",
        lambda: change(User.objects.get(pk=user.pk)),
        after=True,
    )
    client = browser()
    assert submit(client, "auth/password/request", {"email": OWNER}).status_code == 401
    committed_first(changes)
    data = {"key": mailed_code(), "password": PASSWORD + "reset"}
    expected = answer(submit(browser(), "auth/password/reset", data))
    mail.outbox.clear()

    response = submit(client, "auth/password/reset", data)

    assert answer(response) == expected
    assert mail.outbox == []
    user.refresh_from_db()
    assert user.check_password(password)
    assert list(EmailAddress.objects.values_list("email", flat=True)) == addresses


def test_a_reauthentication_code_dies_when_the_account_gains_an_authenticator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = reauthentication_code(monkeypatch)
    mail.outbox.clear()
    with_totp(pending.user)

    pending.client.get("/api/session/")

    assert REAUTHENTICATION_CODE_SESSION_KEY not in pending.client.session
    assert mail.outbox == []


def test_a_reauthentication_code_dies_when_its_address_is_replaced_after_the_code_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = passwordless("owner")
    EmailAddress.objects.create(user=user, email=SECOND, verified=True)
    client = signed_in(user)
    changes = changing(
        monkeypatch,
        ConfirmReauthenticationCodeProcess,
        "send",
        lambda: recreates(SECOND)(user),
        after=True,
    )
    assert submit(client, "auth/reauthenticate/code/request", {"email": SECOND}).status_code == 200
    committed_first(changes)
    data = {"code": mailed_code()}
    expected = answer(submit(signed_in(user), "auth/reauthenticate/code/confirm", data))
    mail.outbox.clear()

    response = submit(client, "auth/reauthenticate/code/confirm", data)

    assert answer(response) == expected
    assert mail.outbox == []


SIGN_INS: dict[str, tuple[Start, object, str]] = {
    "verification": (signup_code, headless, "login_on_verification"),
    "second-factor": (second_factor, AccountAdapter, "login"),
}
SIGN_IN_CHANGES: dict[str, tuple[Callable[[User], None], bool]] = {
    "password-changed": (password_changed, False),
    "deactivated": (deactivated, False),
    "address-removed": (removes(OWNER), True),
}


@pytest.mark.parametrize("change", SIGN_IN_CHANGES)
@pytest.mark.parametrize("completion", SIGN_INS)
def test_a_change_started_while_a_completion_signs_in_commits_after_the_sign_in(
    completion: str, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, owner, name = SIGN_INS[completion]
    changed, keeps_session = SIGN_IN_CHANGES[change]
    pending = start(monkeypatch)
    changes = changing(
        monkeypatch, owner, name, lambda: changed(User.objects.get(pk=pending.user.pk))
    )

    response = submit(pending.client, pending.endpoint, pending.data)

    assert (response.status_code, committed_first(changes)) == (200, False)
    assert (session_user(pending.client) == pending.user.pk) is keeps_session


def pending_provider_signup(monkeypatch: pytest.MonkeyPatch, provider: str) -> Client:
    serve_provider_profile(monkeypatch)
    client = browser()
    complete(client, begin(client, provider), provider)
    return client


def provider_details(provider: str) -> dict[str, str]:
    email = "solar@gmail.com" if provider == "google" else "solar@example.com"
    return {"email": email, "company_name": "Solar"}


def connected_elsewhere(provider: str) -> None:
    other = account("other")
    SocialAccount.objects.create(user=other, provider=provider, uid=f"{provider}-identity")


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_a_pending_provider_signup_dies_when_its_identity_is_connected_elsewhere(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = pending_provider_signup(monkeypatch, provider)
    connected_elsewhere(provider)
    expected = answer(submit(browser(), "auth/provider/signup", provider_details(provider)))

    response = submit(client, "auth/provider/signup", provider_details(provider))

    assert answer(response) == expected
    assert mail.outbox == []
    assert list(User.objects.values_list("username", flat=True)) == ["other"]
    assert session_user(client) is None


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_a_provider_signup_answers_like_nothing_pending_when_its_identity_is_connected_during_it(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = pending_provider_signup(monkeypatch, provider)
    expected = answer(submit(browser(), "auth/provider/signup", provider_details(provider)))
    save = SignupInput.save

    def connected_meanwhile(self: SignupInput, request: Any) -> Any:
        elsewhere(lambda: connected_elsewhere(provider))
        return save(self, request)

    monkeypatch.setattr(SignupInput, "save", connected_meanwhile)

    response = submit(client, "auth/provider/signup", provider_details(provider))

    assert answer(response) == expected
    assert mail.outbox == []
    assert list(User.objects.values_list("username", flat=True)) == ["other"]
    assert session_user(client) is None


def test_a_provider_signup_answers_like_nothing_pending_when_its_address_is_verified_during_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = pending_provider_signup(monkeypatch, "google")
    details = provider_details("google")
    expected = answer(submit(browser(), "auth/provider/signup", details))

    def verify_elsewhere() -> None:
        EmailAddress.objects.create(user=account("other"), email=details["email"], verified=True)

    changes = changing(
        monkeypatch, account_utils, "cleanup_email_addresses", verify_elsewhere, after=True
    )

    response = submit(client, "auth/provider/signup", details)
    committed = committed_first(changes)

    assert answer(response) == expected
    assert mail.outbox == []
    assert list(User.objects.values_list("username", flat=True)) == ["other"]
    assert list(
        EmailAddress.objects.filter(email=details["email"]).values_list(
            "user__username", "verified"
        )
    ) == [("other", True)]
    assert session_user(client) is None
    assert committed


def credentialed_account() -> tuple[User, str]:
    user = account("owner")
    secret = with_totp(user)
    RecoveryCodes.activate(user)
    SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    return user, secret


def second_factor_by(kind: str, monkeypatch: pytest.MonkeyPatch) -> Pending:
    now = time.time()
    totp_clock(monkeypatch, now)
    serve_provider_profile(monkeypatch)
    user, secret = credentialed_account()
    client = browser()
    if kind == "password":
        credentials = {"email": OWNER, "password": PASSWORD}
        assert submit(client, "auth/login", credentials).status_code == 401
    else:
        complete(client, begin(client))
    return Pending(client, "auth/2fa/authenticate", {"code": totp_code(secret, now)}, user)


def updates(model: type[Any], **fields: Any) -> Callable[[User], None]:
    def update(user: User) -> None:
        rows = User.objects.filter(pk=user.pk) if model is User else model.objects.filter(user=user)
        rows.update(**fields)

    return update


def recovery_codes(user: User) -> Authenticator:
    codes: Authenticator = Authenticator.objects.get(
        user=user, type=Authenticator.Type.RECOVERY_CODES
    )
    return codes


def retyped_recovery_codes(user: User) -> None:
    row = recovery_codes(user)
    row.type = Authenticator.Type.WEBAUTHN
    row.save(update_fields=["type"])


def added_address(user: User) -> None:
    EmailAddress.objects.create(user=user, email=SECOND)


def removed_identity(user: User) -> None:
    SocialAccount.objects.filter(user=user).delete()


def removed_recovery_codes(user: User) -> None:
    recovery_codes(user).delete()


def recreated_identity(user: User) -> None:
    SocialAccount.objects.filter(user=user).delete()
    SocialAccount.objects.create(user=user, provider="google", uid="google-identity")


def recreated_totp(user: User) -> None:
    row = Authenticator.objects.get(user=user, type=Authenticator.Type.TOTP)
    row.delete()
    Authenticator.objects.create(user=user, type=Authenticator.Type.TOTP, data=row.data)


def reseeded_recovery_codes(user: User) -> None:
    row = recovery_codes(user)
    row.data = {**row.data, "seed": encrypt(RecoveryCodes.generate_seed())}
    row.save(update_fields=["data"])


def used_recovery_code(user: User) -> None:
    codes = RecoveryCodes(recovery_codes(user))
    assert codes.validate_code(codes.get_unused_codes()[0])


def shown_recovery_codes(user: User) -> None:
    RecoveryCodes(recovery_codes(user)).mark_as_viewed()


STATE_FIELDS: dict[str, Callable[[User], None]] = {
    "account-password": password_changed,
    "account-active": deactivated,
    "account-email": updates(User, email=CHANGED),
    "account-staff": updates(User, is_staff=True),
    "account-superuser": updates(User, is_superuser=True),
    "address-id": recreates(OWNER),
    "address-email": updates(EmailAddress, email=CHANGED),
    "address-verified": updates(EmailAddress, verified=False),
    "address-primary": updates(EmailAddress, primary=False),
    "address-added": added_address,
    "identity-id": recreated_identity,
    "identity-provider": updates(SocialAccount, provider="microsoft"),
    "identity-uid": updates(SocialAccount, uid="other-identity"),
    "identity-removed": removed_identity,
    "authenticator-id": recreated_totp,
    "authenticator-type": retyped_recovery_codes,
    "authenticator-data": reseeded_recovery_codes,
    "authenticator-removed": removed_recovery_codes,
}


@pytest.mark.parametrize("field", STATE_FIELDS)
@pytest.mark.parametrize("kind", ["password", "provider"])
def test_a_change_of_any_credential_field_ends_a_pending_second_factor_step(
    kind: str, field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = second_factor_by(kind, monkeypatch)
    STATE_FIELDS[field](pending.user)
    expected = pending.nothing_pending()

    response = submit(pending.client, pending.endpoint, pending.data)

    assert answer(response) == expected
    assert mail.outbox == []
    assert session_user(pending.client) is None


LEFT_OUT_FIELDS: dict[str, Callable[[User], None]] = {
    "account-last-sign-in": updates(User, last_login=timezone.now()),
    "account-names": updates(User, username="renamed", first_name="Renamed", last_name="Owner"),
    "identity-profile": updates(SocialAccount, extra_data={"email": CHANGED}),
    "identity-last-sign-in": updates(SocialAccount, last_login=timezone.now()),
    "authenticator-last-use": updates(Authenticator, last_used_at=timezone.now()),
    "recovery-code-used": used_recovery_code,
    "recovery-codes-shown": shown_recovery_codes,
}


@pytest.mark.parametrize("field", LEFT_OUT_FIELDS)
@pytest.mark.parametrize("kind", ["password", "provider"])
def test_a_change_of_a_field_outside_the_credential_state_keeps_a_pending_step(
    kind: str, field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = second_factor_by(kind, monkeypatch)
    LEFT_OUT_FIELDS[field](pending.user)

    response = submit(pending.client, pending.endpoint, pending.data)

    assert response.status_code == 200
    assert session_user(pending.client) == pending.user.pk


def test_a_second_factor_completed_with_a_migrated_recovery_code_signs_in() -> None:
    user = account("owner")
    with_totp(user)
    Authenticator.objects.create(
        user=user,
        type=Authenticator.Type.RECOVERY_CODES,
        data={"migrated_codes": [encrypt("12345678"), encrypt("87654321")]},
    )
    client = browser()
    assert submit(client, "auth/login", {"email": OWNER, "password": PASSWORD}).status_code == 401

    response = submit(client, "auth/2fa/authenticate", {"code": "12345678"})

    assert response.status_code == 200
    assert session_user(client) == user.pk
    assert RecoveryCodes(recovery_codes(user)).get_unused_codes() == ["87654321"]


def established_admin(client: Client) -> tuple[int, bool, int]:
    records = client.session.get(AUTHENTICATION_METHODS_SESSION_KEY, [])
    can_admin = client.get("/api/session/").json()["user"]["can_admin"]
    return len(records), can_admin, client.get("/admin/").status_code


@pytest.mark.parametrize("kind", ["totp", "recovery-code"])
def test_a_repeated_second_factor_completion_leaves_the_established_session_signed_in(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    totp_clock(monkeypatch, now)
    user = administrator()
    secret = with_totp(user)
    codes = RecoveryCodes.activate(user).get_unused_codes()
    client = browser()
    credentials = {"email": user.email, "password": TEST_PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 401
    data = {"code": totp_code(secret, now) if kind == "totp" else codes[0]}
    assert submit(client, "auth/2fa/authenticate", data).status_code == 200
    assert established_admin(client) == (2, True, 200)

    status, body = answer(submit(client, "auth/2fa/authenticate", data))

    assert status == 401
    assert (body["code"], body["meta"]) == ("not_authenticated", {"is_authenticated": None})
    assert "user" not in body["data"]
    assert established_admin(client) == (2, True, 200)
    assert session_user(client) == user.pk


def test_a_repeated_provider_second_factor_completion_leaves_the_session_signed_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = provider_second_factor(monkeypatch)
    assert submit(pending.client, pending.endpoint, pending.data).status_code == 200
    records = pending.client.session[AUTHENTICATION_METHODS_SESSION_KEY]

    status, body = answer(submit(pending.client, pending.endpoint, pending.data))

    assert (status, body["code"]) == (401, "not_authenticated")
    assert pending.client.session[AUTHENTICATION_METHODS_SESSION_KEY] == records
    assert session_user(pending.client) == pending.user.pk


def test_a_password_change_started_while_a_reset_code_is_requested_waits_and_ends_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = account("owner")
    changes = changing(
        monkeypatch,
        RequestPasswordResetInput,
        "clean",
        lambda: password_changed(User.objects.get(pk=user.pk)),
        after=True,
    )
    client = browser()
    assert submit(client, "auth/password/request", {"email": OWNER}).status_code == 401
    waited = not committed_first(changes)
    data = {"key": mailed_code(), "password": PASSWORD + "reset"}
    expected = answer(submit(browser(), "auth/password/reset", data))
    mail.outbox.clear()

    response = submit(client, "auth/password/reset", data)

    assert answer(response) == expected
    assert mail.outbox == []
    user.refresh_from_db()
    assert user.check_password(PASSWORD + "changed")
    assert waited


def test_an_address_replacement_started_while_a_reauthentication_code_is_requested_ends_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = passwordless("owner")
    EmailAddress.objects.create(user=user, email=SECOND, verified=True)
    client = signed_in(user)
    changes = changing(
        monkeypatch,
        RequestReauthenticationCodeInput,
        "clean",
        lambda: recreates(SECOND)(user),
        after=True,
    )
    assert submit(client, "auth/reauthenticate/code/request", {"email": SECOND}).status_code == 200
    waited = not committed_first(changes)
    data = {"code": mailed_code()}
    expected = answer(submit(signed_in(user), "auth/reauthenticate/code/confirm", data))
    mail.outbox.clear()

    response = submit(client, "auth/reauthenticate/code/confirm", data)

    assert answer(response) == expected
    assert mail.outbox == []
    assert waited


def test_an_address_replacement_started_while_an_address_change_starts_waits_and_ends_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = account("owner")
    client = signed_in(user, reauthenticated=True)
    changes = changing(
        monkeypatch, AddEmailInput, "clean", lambda: recreates(OWNER)(user), after=True
    )
    assert submit(client, "account/email", {"email": CHANGED}).status_code == 200
    waited = not committed_first(changes)
    data = {"key": mailed_code()}
    expected = answer(submit(signed_in(user), "auth/email/verify", data))
    mail.outbox.clear()

    response = submit(client, "auth/email/verify", data)

    assert answer(response) == expected
    assert mail.outbox == []
    assert list(EmailAddress.objects.values_list("email", flat=True)) == [OWNER]
    assert waited


def test_a_provider_sign_in_whose_identity_is_connected_after_the_callback_looked_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_provider_profile(monkeypatch)
    user = account("owner")
    client = browser()
    state = begin(client)
    changes = changing(
        monkeypatch,
        SocialLogin,
        "lookup",
        lambda: SocialAccount.objects.create(user=user, provider="google", uid="google-identity"),
    )

    response = complete(client, state)

    assert committed_first(changes)
    assert "error=permission_denied" in response["Location"]
    assert session_user(client) is None


@contextmanager
def account_held(user: User) -> Iterator[Callable[[], bool]]:
    # Holds the account's address rows and account row on another connection; the yielded check
    # tells whether they are still held.
    held, release = Event(), Event()

    def hold_the_account() -> bool:
        try:
            with transaction.atomic():
                list(EmailAddress.objects.select_for_update().filter(user=user))
                User.objects.select_for_update().get(pk=user.pk)
                held.set()
                return release.wait(timeout=10)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(1) as pool:
        holder = pool.submit(hold_the_account)
        assert held.wait(timeout=10)
        try:
            yield lambda: not holder.done()
        finally:
            release.set()
        assert holder.result(timeout=10)


def error_codes(response: Any) -> list[str]:
    return [error["code"] for error in response.json().get("errors", [])]


def test_a_sign_in_refused_by_its_budget_waits_for_no_lock() -> None:
    user = account("owner")
    guess = {"email": OWNER, "password": "wrong-" + PASSWORD}
    for _ in range(5):
        assert submit(browser(), "auth/login", guess).status_code == 400

    with account_held(user) as held:
        response = submit(browser(), "auth/login", {"email": OWNER, "password": PASSWORD})
        still_held = held()

    assert still_held
    assert response.status_code == 400
    assert error_codes(response) == ["too_many_login_attempts"]


def spent_reset(user: User) -> Client:
    for _ in range(3):
        assert submit(browser(), "auth/password/request", {"email": OWNER}).status_code == 401
    return browser()


def spent_reauthentication_code(user: User) -> Client:
    user.set_unusable_password()
    user.save(update_fields=["password"])
    client = signed_in(user)
    for _ in range(3):
        response = submit(client, "auth/reauthenticate/code/request", {"email": OWNER})
        assert response.status_code == 200
    return client


def spent_address_change(user: User) -> Client:
    client = signed_in(user, reauthenticated=True)
    for index in range(3):
        response = submit(client, "account/email", {"email": f"new-{index}@example.com"})
        assert response.status_code == 200
    return client


SPENT_BUDGETS: dict[
    str, tuple[Callable[[User], Client], str, dict[str, str], tuple[int, list[str]]]
] = {
    "reset-code": (spent_reset, "auth/password/request", {"email": OWNER}, (429, [])),
    "reauthentication-code": (
        spent_reauthentication_code,
        "auth/reauthenticate/code/request",
        {"email": OWNER},
        (400, ["too_many_login_attempts"]),
    ),
    "address-change": (spent_address_change, "account/email", {"email": CHANGED}, (429, [])),
}


@pytest.mark.parametrize("start", SPENT_BUDGETS)
def test_a_start_refused_by_its_budget_waits_for_no_lock(start: str) -> None:
    spend, endpoint, data, refusal = SPENT_BUDGETS[start]
    user = account("owner")
    client = spend(user)
    mail.outbox.clear()

    with account_held(user) as held:
        response = submit(client, endpoint, data)
        still_held = held()

    assert still_held
    assert (response.status_code, error_codes(response)) == refusal
    assert mail.outbox == []


def test_a_reauthentication_code_admitted_after_its_budget_looked_spent_sends_no_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = passwordless("owner")
    client = signed_in(user)
    monkeypatch.setattr(headless, "spent", lambda *args, **kwargs: True)

    response = submit(client, "auth/reauthenticate/code/request", {"email": OWNER})

    assert response.status_code == 429
    assert mail.outbox == []
    assert REAUTHENTICATION_CODE_SESSION_KEY not in client.session


def test_an_address_change_whose_budget_is_spent_after_it_looked_available_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = account("owner")
    client = spent_address_change(user)
    mail.outbox.clear()
    monkeypatch.setattr(headless, "spent", lambda *args, **kwargs: False)

    response = submit(client, "account/email", {"email": CHANGED})

    assert response.status_code == 429
    assert mail.outbox == []
    assert not EmailAddress.objects.filter(email=CHANGED).exists()


def test_a_reset_requested_while_its_address_moves_resets_the_account_that_now_holds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = account("owner")
    changes = changing(monkeypatch, authentication, "lock_account", lambda: reassigned(user))
    client = browser()

    assert submit(client, "auth/password/request", {"email": OWNER}).status_code == 401
    reset = {"key": mailed_code(), "password": PASSWORD + "reset"}
    response = submit(client, "auth/password/reset", reset)

    assert committed_first(changes)
    assert response.status_code == 401
    assert User.objects.get(username="other").check_password(PASSWORD + "reset")
    assert User.objects.get(pk=user.pk).check_password(PASSWORD)


def test_a_provider_sign_in_whose_identity_moves_before_it_is_locked_signs_in_its_new_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_provider_profile(monkeypatch)
    user = account("owner")
    other = account("other")
    SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    client = browser()
    state = begin(client)
    changes = changing(
        monkeypatch,
        social,
        "lock_account",
        lambda: SocialAccount.objects.filter(uid="google-identity").update(user=other),
    )

    complete(client, state)

    assert committed_first(changes)
    assert session_user(client) == other.pk


def changed_once_steps_are_checked(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[], object]
) -> Changes:
    # Starts the change from another connection once the first POST has had its pending steps
    # checked at the start of the request.
    changes: Changes = []
    discard_stale = processes.discard_stale

    def changed_after_the_check(request: HttpRequest) -> None:
        discard_stale(request)
        if request.method == "POST" and not changes:
            future = alongside(change)
            changes.append((future.done(), future))

    monkeypatch.setattr(processes, "discard_stale", changed_after_the_check)
    return changes


def test_a_change_committed_after_the_request_checked_its_steps_ends_the_second_factor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = second_factor(monkeypatch)
    changes = changed_once_steps_are_checked(
        monkeypatch, lambda: password_changed(User.objects.get(pk=pending.user.pk))
    )

    response = submit(pending.client, pending.endpoint, pending.data)

    assert committed_first(changes)
    assert response.status_code == 401
    assert session_user(pending.client) is None


def test_a_second_factor_reauthentication_without_an_authenticator_refuses_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only an account holding an authenticator reaches the code (others answer 403), so the
    # authenticator is removed elsewhere once the view has checked the account's factor.
    user = account("owner")
    with_totp(user)
    client = browser()
    client.force_login(user)
    checked = security.strongest_factor

    def removed_after_the_check(checked_user: Any) -> Any:
        factor = checked(checked_user)
        elsewhere(lambda: Authenticator.objects.filter(user_id=checked_user.pk).delete())
        return factor

    monkeypatch.setattr("pyracast.accounts.step_up.strongest_factor", removed_after_the_check)

    response = submit(client, "auth/2fa/reauthenticate", {"code": "123456"})

    assert (response.status_code, error_codes(response)) == (400, ["incorrect_code"])


@override_settings(ACCOUNT_EMAIL_VERIFICATION_SUPPORTS_CHANGE=True)
def test_a_signup_that_changes_its_address_while_verifying_completes_with_the_new_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = signup_code(monkeypatch)
    mail.outbox.clear()

    assert submit(pending.client, "account/email", {"email": CHANGED}).status_code == 200
    response = submit(pending.client, pending.endpoint, {"key": mailed_code()})

    assert response.status_code == 200
    assert session_user(pending.client) == pending.user.pk
    assert list(EmailAddress.objects.values_list("email", "verified")) == [(CHANGED, True)]


@override_settings(ACCOUNT_EMAIL_VERIFICATION_SUPPORTS_CHANGE=True)
def test_a_signup_address_change_after_its_account_changed_ends_the_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = signup_code(monkeypatch)
    changes = changed_once_steps_are_checked(
        monkeypatch, lambda: password_changed(User.objects.get(pk=pending.user.pk))
    )
    mail.outbox.clear()

    response = submit(pending.client, "account/email", {"email": CHANGED})

    assert committed_first(changes)
    assert response.status_code == 409
    assert mail.outbox == []
    assert answer(submit(pending.client, pending.endpoint, pending.data)) == (
        pending.nothing_pending()
    )


def started(start: Callable[[HttpRequest], None]) -> HttpRequest:
    request = RequestFactory().post("/")
    request.session = import_module(settings.SESSION_ENGINE).SessionStore()
    processes.starting(request, lambda: start(request))
    return request


STARTED_STEPS: dict[str, tuple[bool, bool, bool]] = {
    "locked": (True, True, True),
    "never-locked": (True, False, False),
    "locked-but-gone": (False, True, False),
}


@pytest.mark.parametrize("case", STARTED_STEPS)
def test_a_started_step_holds_only_for_an_existing_account_its_request_locked(case: str) -> None:
    exists, locked, held = STARTED_STEPS[case]
    user_id = account("owner").pk
    if not exists:
        User.objects.filter(pk=user_id).delete()

    def start(request: HttpRequest) -> None:
        if locked:
            processes.lock_account(user_id)
        request.session[PASSWORD_RESET_VERIFICATION_SESSION_KEY] = {"user_id": str(user_id)}

    request = started(start)

    assert (PASSWORD_RESET_VERIFICATION_SESSION_KEY in request.session) is held
