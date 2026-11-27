from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from allauth.core import context as allauth_context
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.test import Client, RequestFactory, override_settings
from django.utils import translation

from pyracast.accounts.adapters import AccountAdapter
from pyracast.accounts.models import OutboundMail
from tests.support.account_mail import (
    CASES,
    CHANGED_NOTICE,
    FIREFOX,
    FRESH,
    PRIMARY,
    Combination,
    Mail,
    account,
    assert_account_mail,
    assert_security_details,
    browser,
    matrix_settings,
    provider_requests,
    run_case,
)
from tests.support.auth_lifecycle import submit
from tests.support.company_registration import signup
from tests.support.outbox import deliver_mail

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
# These committed requests show that the matrix outcome holds under real commits and the database
# rate-limit cache; they do not show that every row's commit callbacks run in the same order.
FAMILIES = (
    Combination("signup", "verified", "unknown", "signed-out", "none"),
    Combination("provider-signup", "verified", "other-account", "signed-out", "google"),
    Combination("login", "unverified-new", "own-primary", "signed-out", "none"),
    Combination("provider-login", "unverified-returning", "own-primary", "signed-out", "microsoft"),
    Combination("verify", "verified", "none", "pending-change", "none"),
    Combination("resend", "verified", "none", "pending-change", "none"),
    Combination("add-address", "verified", "unknown", "reauthenticated", "none"),
    Combination(
        "remove-address",
        "unverified-secondary",
        "own-unverified-secondary",
        "reauthenticated",
        "none",
    ),
    Combination("reset-request", "verified", "other-account", "signed-in", "none"),
    Combination("reset", "verified", "own-primary", "signed-out", "none"),
    Combination("password", "passwordless", "none", "reauthenticated", "none"),
    Combination("password", "verified", "none", "reauthenticated", "none"),
    Combination(
        "reauthentication-code", "passwordless", "own-primary", "reauthentication-pending", "none"
    ),
    Combination("totp-on", "verified", "none", "reauthenticated", "none"),
    Combination("totp-off", "verified", "none", "reauthenticated", "none"),
    Combination("recovery-codes", "verified", "none", "reauthenticated", "none"),
    Combination("provider-connect", "verified", "none", "reauthenticated", "google"),
    Combination("disconnect", "passwordless-linked", "none", "reauthenticated", "microsoft"),
)
SAMPLE = [next(case for case in CASES if case.combination == family) for family in FAMILIES]


@pytest.fixture(autouse=True)
def committed_mail() -> Iterator[None]:
    with override_settings(**matrix_settings()):
        yield


@pytest.fixture(autouse=True)
def identity(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return provider_requests(monkeypatch)


@pytest.mark.parametrize("case", SAMPLE, ids=str)
def test_committed_request_sends_what_the_matrix_expects(
    case: Any, identity: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert isinstance(case.expected, Mail)
    run_case(case, identity, monkeypatch, Client)


def test_throttled_resend_is_refused_without_mail() -> None:
    client = browser()
    assert signup(client, {"email": FRESH, "company_name": "Throttled"}).status_code == 401
    deliver_mail()
    mail.outbox.clear()

    response = submit(client, "auth/email/verify/resend", {})

    deliver_mail()
    assert (response.status_code, mail.outbox) == (429, [])


def test_account_mail_waits_for_the_commit_and_is_dropped_on_rollback() -> None:
    user = account(PRIMARY, "member", "en")
    context = {
        "user": user,
        "timestamp": datetime.now(UTC),
        "ip": "192.0.2.4",
        "user_agent": FIREFOX,
    }

    def send_and_roll_back() -> None:
        with transaction.atomic():
            AccountAdapter().send_mail(CHANGED_NOTICE, PRIMARY, context)
            assert mail.outbox == []
            raise RuntimeError

    with pytest.raises(RuntimeError):
        send_and_roll_back()

    deliver_mail()
    assert mail.outbox == []
    with transaction.atomic():
        AccountAdapter().send_mail(CHANGED_NOTICE, PRIMARY, context)
        assert mail.outbox == []
    deliver_mail()
    (message,) = mail.outbox
    assert message.to == [PRIMARY]
    assert message.extra_headers["X-Pyracast-Mail"] == CHANGED_NOTICE


@pytest.mark.parametrize(
    ("prefix", "language", "subject"),
    [
        ("mfa/email/webauthn_added", "en", "A passkey was added to your account"),
        ("mfa/email/webauthn_added", "tr", "Hesabınıza bir geçiş anahtarı eklendi"),  # noqa: RUF001 -- Turkish copy.
        ("mfa/email/webauthn_removed", "en", "A passkey was removed from your account"),
        ("mfa/email/webauthn_removed", "tr", "Hesabınızdan bir geçiş anahtarı kaldırıldı"),  # noqa: RUF001 -- Turkish copy.
    ],
)
def test_passkey_security_notice_uses_the_outbox_and_account_language(
    prefix: str, language: str, subject: str
) -> None:
    user = account(PRIMARY, "member", language)
    request = RequestFactory().post("/", headers={"User-Agent": FIREFOX})
    since = datetime.now(UTC)
    request_language = "tr" if language == "en" else "en"

    with allauth_context.request_context(request), translation.override(request_language):
        with transaction.atomic():
            AccountAdapter().send_notification_mail(prefix, user)
            assert not mail.outbox
            assert OutboundMail.objects.get().kind == prefix
            transaction.set_rollback(True)
        assert not OutboundMail.objects.exists()

        with transaction.atomic():
            AccountAdapter().send_notification_mail(prefix, user)
            assert not mail.outbox

    assert deliver_mail() == 1
    (message,) = mail.outbox
    assert isinstance(message, EmailMultiAlternatives)
    assert_account_mail(message, prefix=prefix, to=PRIMARY, language=language, subject=subject)
    assert_security_details(message, language=language, since=since)
