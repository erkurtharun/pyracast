from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from django.core import mail
from django.db import transaction
from django.test import Client, override_settings

from pyracast.accounts.adapters import AccountAdapter
from tests.support.account_mail import (
    CASES,
    CHANGED_NOTICE,
    FIREFOX,
    FRESH,
    PRIMARY,
    Combination,
    Mail,
    account,
    browser,
    matrix_settings,
    provider_requests,
    run_case,
)
from tests.support.auth_lifecycle import submit
from tests.support.company_registration import signup

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
        "reauthentication-code",
        "verified-secondary",
        "own-verified-secondary",
        "reauthentication-pending",
        "none",
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
    mail.outbox.clear()

    response = submit(client, "auth/email/verify/resend", {})

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

    assert mail.outbox == []
    with transaction.atomic():
        AccountAdapter().send_mail(CHANGED_NOTICE, PRIMARY, context)
        assert mail.outbox == []
    (message,) = mail.outbox
    assert message.to == [PRIMARY]
    assert message.extra_headers["X-Pyracast-Mail"] == CHANGED_NOTICE
