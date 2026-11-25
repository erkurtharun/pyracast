import time
from collections.abc import Callable, Iterator
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal import auth as totp_auth
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from allauth.usersessions.models import UserSession
from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import Client, override_settings
from django.utils import timezone

from pyracast.accounts import adapters, mail_intents
from pyracast.accounts.models import OutboundMail, UserLanguagePreference
from pyracast.notifications.outbox import publish_due_mail, send_outbound_mail
from tests.support.account_sessions import (
    answer,
    browser,
    capture_security_events,
    session_user,
)
from tests.support.admin_mfa_bootstrap import (
    NEW_PASSWORD,
    TEST_PASSWORD,
    Answer,
    Terminal,
    administrator,
    enrollment_secret,
    run,
)
from tests.support.auth_lifecycle import mailed_code, submit, totp_code
from tests.support.outbox import deliver_mail
from tests.support.step_up import holds_current_authority

pytestmark = pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
NOTICE = "account/email/second_factor_reset"


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


@pytest.fixture
def events(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], list[dict[str, Any]]]:
    return capture_security_events(caplog, monkeypatch)


def customer() -> User:
    user = User.objects.create_user(
        username="customer", email="customer@example.com", password=TEST_PASSWORD
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


def with_factors(user: User) -> list[str]:
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    return RecoveryCodes.activate(user).generate_codes()


def session_of(user: User) -> Client:
    client = Client()
    client.force_login(user)
    assert client.get("/api/session/").json()["user"]["id"] == user.pk
    return client


def state(user: User) -> tuple[Any, ...]:
    return (
        list(Authenticator.objects.filter(user=user).values_list("pk", flat=True)),
        sorted(UserSession.objects.filter(user=user).values_list("session_key", flat=True)),
    )


def notices() -> list[tuple[Any, ...]]:
    return list(
        OutboundMail.objects.order_by("pk").values_list(
            "kind", "recipient", "context", "sealed_code", "status"
        )
    )


def mailed() -> list[Any]:
    deliver_mail()
    return list(mail.outbox)


@pytest.mark.parametrize("make", [administrator, customer], ids=["administrator", "customer"])
def test_a_reset_removes_every_factor_ends_every_session_and_tells_the_account(
    make: Callable[[], User], events: Callable[[], list[dict[str, Any]]]
) -> None:
    user = make()
    codes = with_factors(user)
    sessions = [session_of(user), session_of(user)]
    bystander = customer() if make is administrator else administrator()
    with_factors(bystander)
    kept = session_of(bystander)
    signed_in = len(events())

    shown, terminal = run("reset_mfa", [user.email.upper()], email=user.email, reason="lost_device")

    assert not Authenticator.objects.filter(user=user).exists()
    assert Authenticator.objects.filter(user=bystander).count() == 2
    assert [client.get("/api/session/").json()["user"] for client in sessions] == [None, None]
    assert kept.get("/api/session/").json()["user"]["id"] == bystander.pk
    assert terminal.asked == 1
    assert f"Removed 2 second factors and ended 2 sessions of {user.email}." in shown
    written = events()[signed_in:]
    assert [(line["event"], line.get("reason"), line.get("count")) for line in written] == [
        ("sessions_revoked", "mfa_reset", 2),
        ("mfa_reset_by_operator", "lost_device", 2),
    ]
    assert all(line["user_id"] == user.pk for line in written)
    assert mail.outbox == []
    assert notices() == [(NOTICE, user.email, {}, "", "pending")]
    [message] = mailed()
    assert message.to == [user.email]
    assert message.extra_headers["X-Pyracast-Mail"] == "account/email/second_factor_reset"
    for secret in codes:
        assert secret not in shown
        assert secret not in str(message.body)


def test_the_reset_notice_is_published_by_the_dispatcher_in_the_account_language() -> None:
    user = customer()
    with_factors(user)
    UserLanguagePreference.objects.create(user=user, language="tr")
    run("reset_mfa", [user.email], email=user.email, reason="lost_device")
    published: list[int] = []
    now = timezone.now()

    assert publish_due_mail(published.append, now) == 1
    assert [send_outbound_mail(mail_id, now) for mail_id in published] == [True]

    [message] = mail.outbox
    assert message.to == [user.email]
    assert (
        message.subject
        == "Pyracast iki ad\u0131ml\u0131 do\u011frulaman\u0131z\u0131 kald\u0131rd\u0131"
    )
    assert notices() == [(NOTICE, user.email, {}, "", "sent")]


def test_a_reset_that_fails_after_storing_its_notice_keeps_everything_and_mails_nothing(
    monkeypatch: pytest.MonkeyPatch, events: Callable[[], list[dict[str, Any]]]
) -> None:
    user = customer()
    with_factors(user)
    client = session_of(user)
    before = state(user)
    signed_in = len(events())
    store = mail_intents.enqueue_mail

    def stored_then_failed(*args: Any, **kwargs: Any) -> None:
        store(*args, **kwargs)
        raise DatabaseError("The command's transaction failed after the notice was stored.")

    monkeypatch.setattr(adapters, "enqueue_mail", stored_then_failed)

    with pytest.raises(DatabaseError):
        run("reset_mfa", [user.email], email=user.email, reason="compromised")

    assert state(user) == before
    assert client.get("/api/session/").json()["user"]["id"] == user.pk
    assert events()[signed_in:] == []
    assert notices() == []
    assert mailed() == []


def test_an_administrator_without_a_factor_is_enrolled_again_after_a_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = time.time()
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now))
    user = administrator()
    with_factors(user)
    run("reset_mfa", [user.email], email=user.email, reason="compromised")

    run(
        "bootstrap_admin",
        [NEW_PASSWORD, NEW_PASSWORD, lambda shown: totp_code(enrollment_secret(shown), now)],
        email=user.email,
    )

    assert Authenticator.objects.filter(user=user, type="totp").count() == 1


def test_a_reset_ends_the_pending_steps_of_the_account(monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now))
    user = customer()
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    reset = browser()
    assert submit(reset, "auth/password/request", {"email": user.email}).status_code == 401
    code = mailed_code()
    sign_in = browser()
    credentials = {"email": user.email, "password": TEST_PASSWORD}
    assert submit(sign_in, "auth/login", credentials).status_code == 401
    run("reset_mfa", [user.email], email=user.email, reason="compromised")
    deliver_mail()
    mail.outbox.clear()
    new_password = {"key": code, "password": NEW_PASSWORD}
    second_factor = {"code": totp_code(secret, now)}
    nothing_pending = (
        answer(submit(browser(), "auth/password/reset", new_password)),
        answer(submit(browser(), "auth/2fa/authenticate", second_factor)),
    )

    completed = (
        answer(submit(reset, "auth/password/reset", new_password)),
        answer(submit(sign_in, "auth/2fa/authenticate", second_factor)),
    )

    assert completed == nothing_pending
    user.refresh_from_db()
    assert user.check_password(TEST_PASSWORD)
    assert (session_user(reset), session_user(sign_in)) == (None, None)
    assert mailed() == []


def test_after_a_reset_a_sign_in_records_the_state_the_reset_left() -> None:
    user = customer()
    with_factors(user)
    run("reset_mfa", [user.email], email=user.email, reason="lost_device")
    client = browser()
    credentials = {"email": user.email, "password": TEST_PASSWORD}

    assert submit(client, "auth/login", credentials).status_code == 200

    assert holds_current_authority(client, user.pk)
    change = {"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
    assert submit(client, "account/password/change", change).json()["meta"]["password_changed"]
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)


def test_a_dry_run_changes_nothing_and_asks_nothing(
    events: Callable[[], list[dict[str, Any]]],
) -> None:
    user = customer()
    with_factors(user)
    client = session_of(user)
    before = state(user)
    signed_in = len(events())

    shown, terminal = run("reset_mfa", [], email=user.email, reason="lost_device", dry_run=True)

    assert state(user) == before
    assert client.get("/api/session/").json()["user"]["id"] == user.pk
    assert terminal.asked == 0
    assert shown.strip() == (
        f"Dry run: would remove 2 second factors and end 1 session of {user.email}. "
        "Nothing was changed."
    )
    assert events()[signed_in:] == []
    assert notices() == []
    assert mailed() == []


def refusal(email: str, answers: list[Answer], **options: object) -> tuple[str, Terminal]:
    shown = StringIO()
    terminal = Terminal(shown, list(answers))
    with pytest.raises(CommandError) as refused:
        call_command(
            "reset_mfa",
            email=email,
            reason="lost_device",
            stdin=terminal,
            stdout=shown,
            stderr=StringIO(),
            **options,
        )
    return str(refused.value), terminal


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_reset_of_an_unknown_or_factorless_account_says_why(
    dry_run: bool, events: Callable[[], list[dict[str, Any]]]
) -> None:
    user = customer()

    unknown, _ = refusal("nobody@example.com", ["nobody@example.com"], dry_run=dry_run)
    factorless, terminal = refusal(user.email, [user.email], dry_run=dry_run)

    assert unknown == "No account uses this address."
    assert factorless == "This account has no second factor. Nothing was changed."
    assert terminal.asked == 0
    assert events() == []
    assert notices() == []
    assert mailed() == []


def test_a_second_reset_finds_nothing_to_reset() -> None:
    user = customer()
    with_factors(user)
    run("reset_mfa", [user.email], email=user.email, reason="lost_device")
    deliver_mail()
    mail.outbox.clear()

    message, _terminal = refusal(user.email, [user.email])

    assert message == "This account has no second factor. Nothing was changed."
    assert [notice[0] for notice in notices()] == [NOTICE]
    assert mailed() == []


@pytest.mark.parametrize("answer", ["", "someone.else@example.com"])
def test_a_reset_needs_the_address_typed_again(
    answer: str, events: Callable[[], list[dict[str, Any]]]
) -> None:
    user = customer()
    with_factors(user)
    client = session_of(user)
    before = state(user)
    signed_in = len(events())

    message, _terminal = refusal(user.email, [answer])

    assert message == "The confirmation does not match the address. Nothing was changed."
    assert state(user) == before
    assert client.get("/api/session/").json()["user"]["id"] == user.pk
    assert events()[signed_in:] == []
    assert notices() == []
    assert mailed() == []


def test_an_address_moved_to_another_account_during_the_confirmation_stops_the_reset(
    events: Callable[[], list[dict[str, Any]]],
) -> None:
    first = customer()
    with_factors(first)
    address = first.email
    second = User.objects.create_user(
        username="second", email="second@example.com", password=TEST_PASSWORD
    )
    EmailAddress.objects.create(user=second, email=second.email, primary=True, verified=True)
    with_factors(second)
    kept = session_of(second)
    before = state(second)
    signed_in = len(events())

    def address_moved_meanwhile(_shown: str) -> str:
        EmailAddress.objects.filter(user=first).delete()
        User.objects.filter(pk=first.pk).update(email="")
        EmailAddress.objects.create(user=second, email=address, verified=True)
        return address

    message, _terminal = refusal(address, [address_moved_meanwhile])

    assert message == (
        "The account changed while the command ran. Nothing was changed; run it again."
    )
    assert state(second) == before
    assert Authenticator.objects.filter(user=first).count() == 2
    assert kept.get("/api/session/").json()["user"]["id"] == second.pk
    assert events()[signed_in:] == []
    assert notices() == []
    assert mailed() == []


def test_a_reset_refuses_an_invalid_address_and_a_shared_one() -> None:
    first = customer()
    other = User.objects.create_user(username="other", email=first.email.upper())
    EmailAddress.objects.create(user=other, email=first.email.upper(), primary=True)

    invalid, _ = refusal("not-an-address", [])
    shared, _ = refusal(first.email, [first.email])

    assert invalid == "Enter a valid email address."
    assert shared == "The address belongs to more than one account. Resolve that first."


def test_a_reset_keeps_a_password_out_of_its_output(caplog: pytest.LogCaptureFixture) -> None:
    user = customer()
    with_factors(user)

    shown, _terminal = run("reset_mfa", [user.email], email=user.email, reason="compromised")

    assert TEST_PASSWORD not in shown
    assert TEST_PASSWORD not in caplog.text
