import time
from collections.abc import Callable
from io import StringIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import segno
from allauth.account.internal.flows.login import record_authentication
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal import auth as totp_auth
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, RequestFactory

from pyracast.accounts.mfa import MFAAdapter
from tests.support.account_sessions import capture_security_events
from tests.support.admin_mfa_bootstrap import (
    NEW_PASSWORD,
    TEST_PASSWORD,
    Answer,
    Terminal,
    administrator,
    enrollment_secret,
    run,
    setup_link,
)
from tests.support.auth_lifecycle import totp_code

pytestmark = pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
ADDRESS = "first.admin@example.com"


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> float:
    moment = time.time()
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: moment))
    return moment


@pytest.fixture
def events(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], list[dict[str, Any]]]:
    return capture_security_events(caplog, monkeypatch)


def current_code(now: float) -> Callable[[str], str]:
    return lambda shown: totp_code(enrollment_secret(shown), now)


def guided(now: float, password: str = NEW_PASSWORD) -> list[Answer]:
    return [password, password, current_code(now)]


def snapshot() -> tuple[list[Any], ...]:
    return (
        list(
            User.objects.order_by("pk").values_list(
                "pk", "username", "email", "password", "is_active", "is_staff", "is_superuser"
            )
        ),
        list(EmailAddress.objects.order_by("pk").values_list("user_id", "email", "primary")),
        list(Authenticator.objects.order_by("pk").values_list("user_id", "type", "data")),
    )


def refusal(
    answers: list[Answer], email: str = ADDRESS, **options: object
) -> tuple[str, str, Terminal]:
    shown = StringIO()
    terminal = Terminal(shown, answers)
    with pytest.raises(CommandError) as refused:
        call_command(
            "bootstrap_admin",
            email=email,
            stdin=terminal,
            stdout=shown,
            stderr=StringIO(),
            **options,
        )
    return str(refused.value), shown.getvalue(), terminal


def test_the_first_administrator_is_created_from_the_operator_terminal(
    now: float, events: Callable[[], list[dict[str, Any]]], caplog: pytest.LogCaptureFixture
) -> None:
    with patch.object(segno, "make_qr", wraps=segno.make_qr) as encode:
        shown, terminal = run("bootstrap_admin", guided(now), email=" First.Admin@Example.com ")

    encode.assert_called_once_with(setup_link(shown))
    user = User.objects.get()
    assert (user.email, user.is_active, user.is_staff, user.is_superuser) == (
        ADDRESS,
        True,
        True,
        True,
    )
    assert user.username != ADDRESS
    assert user.check_password(NEW_PASSWORD)
    assert list(EmailAddress.objects.values_list("user_id", "email", "primary", "verified")) == [
        (user.pk, ADDRESS, True, False)
    ]
    secret = enrollment_secret(shown)
    factor = Authenticator.objects.get(user=user, type="totp")
    assert secret not in str(factor.data)
    assert MFAAdapter().decrypt(factor.data["secret"]) == secret
    assert Authenticator.objects.filter(user=user, type="recovery_codes").count() == 1
    assert " ".join(secret[index : index + 4] for index in range(0, len(secret), 4)) in shown
    assert "issuer=Pyracast" in shown
    assert any(block in shown for block in "▀▄█")
    assert "\x1b" not in shown
    assert f"Platform administrator {ADDRESS} is ready. Username: {user.username}." in shown
    assert terminal.asked == 3
    assert events() == [
        {
            "event": "admin_mfa_provisioned",
            "user_id": user.pk,
            "client_ip": None,
            "reason": "created",
        }
    ]
    assert NEW_PASSWORD not in caplog.text
    assert secret not in caplog.text


def test_an_administrator_without_a_second_factor_is_enrolled_again(
    now: float, events: Callable[[], list[dict[str, Any]]]
) -> None:
    user = administrator()
    User.objects.filter(pk=user.pk).update(is_superuser=False)

    shown, _terminal = run("bootstrap_admin", guided(now), email=user.email)

    user.refresh_from_db()
    assert (user.is_staff, user.is_superuser) == (True, True)
    assert user.check_password(NEW_PASSWORD)
    assert Authenticator.objects.filter(user=user, type="totp").exists()
    assert list(EmailAddress.objects.values_list("email", "verified")) == [(user.email, True)]
    assert f"Platform administrator {user.email} is ready." in shown
    assert [(line["event"], line["reason"]) for line in events()] == [
        ("admin_mfa_provisioned", "enrolled")
    ]


@pytest.mark.parametrize("existing", [False, True])
def test_a_dry_run_changes_nothing_and_asks_nothing(
    existing: bool, events: Callable[[], list[dict[str, Any]]]
) -> None:
    address = administrator().email if existing else ADDRESS
    before = snapshot()

    shown, terminal = run("bootstrap_admin", [], email=address, dry_run=True)

    assert snapshot() == before
    assert terminal.asked == 0
    assert shown.startswith("Dry run: would ")
    assert shown.rstrip().endswith("Nothing was changed.")
    assert "otpauth://" not in shown
    assert events() == []


def customer() -> User:
    user = User.objects.create_user(username="customer", email=ADDRESS, password=TEST_PASSWORD)
    EmailAddress.objects.create(user=user, email=ADDRESS, primary=True, verified=True)
    return user


def inactive() -> User:
    user = administrator()
    User.objects.filter(pk=user.pk).update(is_active=False, email=ADDRESS)
    EmailAddress.objects.filter(user=user).update(email=ADDRESS)
    return user


def enrolled() -> User:
    user = administrator()
    User.objects.filter(pk=user.pk).update(email=ADDRESS)
    EmailAddress.objects.filter(user=user).update(email=ADDRESS)
    Authenticator.objects.create(user=user, type="totp", data={})
    return user


def shared() -> User:
    customer()
    other = User.objects.create_user(username="other", email=ADDRESS.upper())
    EmailAddress.objects.create(user=other, email=ADDRESS.upper(), primary=True)
    return other


ACCOUNTS: dict[str, tuple[Callable[[], object], str]] = {
    "customer": (
        customer,
        "The address belongs to a customer account. Use an address of its own for the "
        "administrator.",
    ),
    "inactive": (inactive, "The account with this address is inactive. Nothing was changed."),
    "enrolled": (
        enrolled,
        "This administrator already has a second factor. Reset it with reset_mfa first.",
    ),
    "shared": (
        shared,
        "The address belongs to more than one account. Resolve that first.",
    ),
}


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("dry_run", [False, True])
def test_an_ineligible_account_is_refused_with_its_reason(
    account: str, dry_run: bool, now: float, events: Callable[[], list[dict[str, Any]]]
) -> None:
    create, reason = ACCOUNTS[account]
    create()
    before = snapshot()

    message, shown, terminal = refusal(guided(now), dry_run=dry_run)

    assert message == reason
    assert (shown, terminal.asked) == ("", 0)
    assert snapshot() == before
    assert events() == []


@pytest.mark.parametrize("address", ["not-an-address", "x" * 245 + "@example.com", ""])
def test_an_invalid_address_is_refused(address: str, now: float) -> None:
    message, _shown, terminal = refusal(guided(now), email=address)

    assert message == "Enter a valid email address."
    assert terminal.asked == 0
    assert not User.objects.exists()


ANSWERS: dict[str, tuple[Callable[[float], list[Answer]], str]] = {
    "passwords differ": (
        lambda now: [NEW_PASSWORD, NEW_PASSWORD + "x", current_code(now)],
        "The two passwords differ. Nothing was stored.",
    ),
    "wrong code": (
        lambda now: [NEW_PASSWORD, NEW_PASSWORD, lambda shown: "000000"],
        "The code does not match the new authenticator. Nothing was stored.",
    ),
    "no code": (
        lambda now: [NEW_PASSWORD, NEW_PASSWORD],
        "The code does not match the new authenticator. Nothing was stored.",
    ),
}


@pytest.mark.parametrize("answers", ANSWERS)
def test_a_wrong_answer_stores_nothing(
    answers: str,
    now: float,
    events: Callable[[], list[dict[str, Any]]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    make, reason = ANSWERS[answers]

    message, _shown, _terminal = refusal(make(now))

    assert message == reason
    assert not User.objects.exists()
    assert not Authenticator.objects.exists()
    assert events() == []
    assert NEW_PASSWORD not in message
    assert NEW_PASSWORD not in caplog.text


@pytest.mark.parametrize(
    "password", ["short-secret", "1q2w3e4r5t6y7u8i", "first.admin@example.com"]
)
def test_a_password_outside_the_policy_is_refused_without_a_bypass(
    password: str, now: float
) -> None:
    message, shown, terminal = refusal(guided(now, password))

    assert message.startswith("The password does not meet the password policy. Nothing was stored.")
    assert password not in message
    assert "otpauth://" not in shown
    assert terminal.asked == 2
    assert not User.objects.exists()


def test_an_account_created_meanwhile_stops_the_bootstrap(
    now: float, events: Callable[[], list[dict[str, Any]]]
) -> None:
    def code_while_another_run_finishes(shown: str) -> str:
        customer()
        return current_code(now)(shown)

    message, _shown, _terminal = refusal(
        [NEW_PASSWORD, NEW_PASSWORD, code_while_another_run_finishes]
    )

    assert message == (
        "The address belongs to a customer account. Use an address of its own for the "
        "administrator."
    )
    assert list(User.objects.values_list("username", flat=True)) == ["customer"]
    assert not Authenticator.objects.exists()
    assert events() == []


def test_an_administrator_created_meanwhile_stops_the_bootstrap(now: float) -> None:
    def code_while_another_run_finishes(shown: str) -> str:
        User.objects.create_user(username="raced", email=ADDRESS, is_staff=True, is_superuser=True)
        return current_code(now)(shown)

    message, _shown, _terminal = refusal(
        [NEW_PASSWORD, NEW_PASSWORD, code_while_another_run_finishes]
    )

    assert message == (
        "The account changed while the command ran. Nothing was stored; run it again."
    )
    assert not User.objects.get().has_usable_password()
    assert not Authenticator.objects.exists()


def test_a_factor_added_meanwhile_stops_the_enrollment(now: float) -> None:
    user = administrator()

    def code_while_a_factor_is_added(shown: str) -> str:
        Authenticator.objects.create(user=user, type="totp", data={})
        return current_code(now)(shown)

    message, _shown, _terminal = refusal(
        [NEW_PASSWORD, NEW_PASSWORD, code_while_a_factor_is_added], email=user.email
    )

    assert (
        message == "This administrator already has a second factor. Reset it with reset_mfa first."
    )
    assert Authenticator.objects.filter(user=user).count() == 1
    user.refresh_from_db()
    assert user.check_password(TEST_PASSWORD)


def test_a_console_without_a_terminal_font_still_gets_the_enrollment_link(now: float) -> None:
    class AsciiConsole(StringIO):
        def write(self, value: str) -> int:
            value.encode("ascii")
            return super().write(value)

    shown = AsciiConsole()
    call_command(
        "bootstrap_admin",
        email=ADDRESS,
        stdin=Terminal(shown, guided(now)),
        stdout=shown,
        stderr=StringIO(),
    )

    assert "This console cannot display the QR code" in shown.getvalue()
    secret = enrollment_secret(shown.getvalue())
    factor = Authenticator.objects.get(type="totp")
    assert MFAAdapter().decrypt(factor.data["secret"]) == secret


def test_a_password_typed_at_a_terminal_is_not_echoed(
    now: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    typed: list[str] = []

    def hidden(prompt: str = "Password: ", stream: object = None) -> str:
        typed.append(prompt)
        return NEW_PASSWORD

    class Keyboard(Terminal):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("getpass.getpass", hidden)
    shown = StringIO()
    keyboard = Keyboard(shown, [current_code(now)])
    call_command("bootstrap_admin", email=ADDRESS, stdin=keyboard, stdout=shown, stderr=StringIO())

    assert len(typed) == 2
    assert NEW_PASSWORD not in shown.getvalue()
    assert User.objects.get().check_password(NEW_PASSWORD)


def test_admin_password_alone_cannot_bootstrap_an_authenticator() -> None:
    user = administrator()
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/session/").json()["csrf_token"]
    response = client.post(
        "/api/auth/browser/v1/auth/login",
        {"email": user.email, "password": TEST_PASSWORD},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 400
    assert client.get("/api/session/").json()["user"] is None
    assert not Authenticator.objects.filter(user=user).exists()
    assert client.get("/admin/").status_code == 302


@pytest.mark.parametrize("method", ["get", "post", "delete"])
def test_authenticated_admin_cannot_use_customer_totp_management(method: str) -> None:
    user = administrator()
    client = Client()
    client.force_login(user)
    response = getattr(client, method)("/api/auth/browser/v1/account/authenticators/totp")
    assert response.status_code == 403
    assert not Authenticator.objects.filter(user=user).exists()


def test_verified_admin_uses_common_password_change_and_strict_csp() -> None:
    user = administrator()
    authenticator = Authenticator.objects.create(user=user, type="totp", data={})
    client = Client()
    client.force_login(user)
    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session
    record_authentication(request, user, method="mfa", type="totp", id=authenticator.pk)
    request.session.save()
    response = client.get("/admin/")
    assert response.status_code == 200
    policy = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in policy
    assert "object-src 'none'" in policy
    assert "unsafe-inline" not in policy
    assert client.get("/admin/login/").headers["Location"] == "/admin/"
    assert client.get("/admin/password_change/").headers["Location"] == "/app/account/"
