from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from allauth.socialaccount.models import SocialAccount
from django.core import mail
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.test import Client, TestCase, override_settings

from pyracast.accounts.account_mail import device_label
from pyracast.accounts.adapters import AccountAdapter
from tests.support.account_mail import (
    CASES,
    CHANGE,
    ENDPOINTS,
    EXPECTED,
    FIREFOX,
    GATED,
    HOSTILE_AGENT,
    LANGUAGE_RULE,
    LANGUAGES,
    NOT_APPLICABLE,
    OTHER,
    PRIMARY,
    RESET_CODE,
    SIGNED,
    Case,
    Mail,
    NoMail,
    account,
    assert_code_mail,
    assume_identity,
    browser,
    gated,
    html,
    matrix_settings,
    one_minute_later,
    only_mail,
    provider_requests,
    request_reauthentication_code,
    row_of,
    run_case,
    through_provider,
)
from tests.support.auth_lifecycle import PASSWORD, member, submit
from tests.support.company_registration import signup
from tests.support.outbox import deliver_mail

pytestmark = pytest.mark.django_db
# Session gates precede account rules; otherwise a strictly narrower row wins.
# These EXPECTED row pairs declare the remaining input/reauthentication precedence.
PRECEDENCE_EXCEPTIONS: dict[tuple[str, int, int], str] = {
    ("reset-request", 15, 17): "an inactive owner gets no reset code regardless of client state",
    (
        "reset-request",
        16,
        17,
    ): "an unverified owner uses request language regardless of client state",
    ("add-address", 38, 39): "an existing address is refused before reauthentication",
    ("add-address", 39, 40): "adding an address requires reauthentication before sending",
    ("remove-address", 44, 47): "the primary address is refused before reauthentication",
    ("remove-address", 45, 47): "an address outside the account is refused before reauthentication",
    ("remove-address", 47, 48): "removing a secondary requires reauthentication before sending",
    ("reauthentication-code", 54, 55): "an account with a password is refused before the address",
    ("reauthentication-code", 54, 56): "an account with a password is refused before the address",
    ("totp-on", 57, 59): "an unverified address prevents activation before reauthentication",
    ("disconnect", 68, 69): "the last way to sign in is refused before reauthentication",
}


class CommitClient(Client):
    def request(self, **request: Any) -> Any:
        # A rolled-back test transaction never commits: run the request's commit callbacks once
        # the request finishes.
        with TestCase.captureOnCommitCallbacks(execute=True):
            return super().request(**request)


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    # The authentication cache needs an autocommit connection, which a rolled-back test
    # transaction does not have; the rate limits stay those of ACCOUNT_RATE_LIMITS.
    caches = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    with override_settings(**matrix_settings(), CACHES=caches):
        cache.clear()
        yield
        cache.clear()


@pytest.fixture(params=LANGUAGES)
def language(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(autouse=True)
def identity(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return provider_requests(monkeypatch)


def commit_browser(language: str | None = None) -> Client:
    return browser(language, CommitClient)


@pytest.mark.parametrize("case", CASES, ids=str)
def test_account_mail_matrix(
    case: Case, identity: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    run_case(case, identity, monkeypatch, CommitClient)


def test_every_combination_is_declared_or_excluded_by_a_rule() -> None:
    assert [str(case) for case in CASES if case.expected is None] == []
    assert all(reason for reason, _ in NOT_APPLICABLE)
    assert LANGUAGE_RULE


def test_overlapping_rows_follow_declared_precedence() -> None:
    ambiguous: set[tuple[str, int, int]] = set()
    for case in CASES:
        matches = [i for i, row in enumerate(EXPECTED) if row.matches(case.combination)]
        first = EXPECTED[matches[0]]
        if gated(case.combination):
            assert isinstance(first.outcome, NoMail)
            assert first.states is not None
            assert first.states <= GATED[case.combination.endpoint]
            continue
        for index in matches[1:]:
            later = EXPECTED[index]
            constraints = (
                (first.shapes, later.shapes),
                (first.targets, later.targets),
                (first.states, later.states),
                (first.providers, later.providers),
            )
            more_specific = all(
                b is None or (a is not None and a <= b) for a, b in constraints
            ) and any(a != b for a, b in constraints)
            if not more_specific:
                ambiguous.add((case.combination.endpoint, matches[0], index))
    assert ambiguous == set(PRECEDENCE_EXCEPTIONS)
    assert all(PRECEDENCE_EXCEPTIONS.values())


def test_every_sending_row_runs_in_both_languages_for_each_provider() -> None:
    languages: dict[tuple[int, str], set[str]] = {}
    for case in CASES:
        key = (row_of(case.combination), case.combination.provider)
        languages.setdefault(key, set()).add(case.language)
    sending = {
        key: seen for key, seen in languages.items() if isinstance(EXPECTED[key[0]].outcome, Mail)
    }
    assert sending
    assert {key: seen for key, seen in sending.items() if seen != set(LANGUAGES)} == {}
    assert {
        provider for (index, provider) in sending if ENDPOINTS[EXPECTED[index].endpoint].provider
    } == {"google", "microsoft"}


def test_reset_request_for_duplicate_unverified_identities_mails_the_shared_address(
    language: str,
) -> None:
    for name in ("first", "second"):
        account("shared@example.com", name, OTHER[language], verified=False)

    response = submit(
        commit_browser(language), "auth/password/request", {"email": "shared@example.com"}
    )

    assert response.status_code == 401
    assert_code_mail(
        only_mail(), prefix=RESET_CODE, to="shared@example.com", language=language, minutes=10
    )


def signed_in_actor(state: str, language: str) -> Client:
    user = account(PRIMARY, "member", language)
    client = commit_browser()
    if state == "reauthentication-pending":
        user.set_unusable_password()
        user.save(update_fields=["password"])
        client.force_login(user)
        request_reauthentication_code(client)
        mail.outbox.clear()
        return client
    credentials = {"email": PRIMARY, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 200
    if state != "signed-in":
        assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    if state == "pending-change":
        assert submit(client, "account/email", {"email": CHANGE}).status_code == 200
    deliver_mail()
    mail.outbox.clear()
    return client


@pytest.mark.parametrize("provider", ["google", "microsoft"])
@pytest.mark.parametrize("state", sorted(SIGNED))
def test_a_signed_in_provider_sign_in_is_refused_and_mails_nothing(
    language: str,
    provider: str,
    state: str,
    identity: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked = account("linked@example.com", "linked", language, verified=False)
    SocialAccount.objects.create(user=linked, provider=provider, uid="linked-identity")
    client = signed_in_actor(state, OTHER[language])
    one_minute_later(monkeypatch)
    assume_identity(identity, "linked-identity", "linked@example.com")

    response = through_provider(client, provider, "login")

    assert response.status_code == 403
    assert mail.outbox == []


def test_verification_resend_after_signup_with_a_taken_address_mails_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account(PRIMARY, "member", "en")
    client = commit_browser()
    assert signup(client, {"email": PRIMARY}).status_code == 401
    deliver_mail()
    mail.outbox.clear()
    one_minute_later(monkeypatch)

    response = submit(client, "auth/email/verify/resend", {})

    assert response.status_code == 200
    deliver_mail()
    assert mail.outbox == []


def test_unrouted_reauthentication_code_resend_answers_404_and_mails_nothing() -> None:
    user = account(PRIMARY, "member", "en")
    user.set_unusable_password()
    user.save(update_fields=["password"])
    client = commit_browser()
    client.force_login(user)
    request_reauthentication_code(client)

    response = submit(client, "auth/reauthenticate/code/resend", {})

    assert response.status_code == 404
    deliver_mail()
    assert mail.outbox == []


def test_email_verified_notice_without_a_trigger_renders_the_product_copy() -> None:
    # allauth 65 ships this template but no flow sends it, so no view can drive it.
    user = member()
    message = AccountAdapter().render_mail(
        "account/email/email_confirm",
        user.email,
        {
            "user": user,
            "timestamp": datetime(2026, 11, 15, 15, 0, tzinfo=timezone(timedelta(hours=3))),
            "ip": "192.0.2.4",
            "user_agent": '<script>alert("unsafe")</script>',
        },
    )

    assert isinstance(message, EmailMultiAlternatives)
    assert message.subject == "Your email address was verified"
    assert message.extra_headers["X-Pyracast-Mail"] == "account/email/email_confirm"
    assert "The email address of your Pyracast account was verified." in str(message.body)
    assert "<script>" not in html(message)
    for content in (str(message.body), html(message)):
        assert "15 November 2026 12:00 UTC" in content
        assert "192.0.2.4" in content


@pytest.mark.parametrize(
    ("user_agent", "label"),
    [
        (FIREFOX, "Firefox on Windows"),
        (HOSTILE_AGENT, "Firefox on Windows"),
        (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/137.0 Safari/537.36",
            "Chrome on Linux",
        ),
        ("curl/8.0 (Android)", "Android"),
        ("Mozilla/5.0 Gecko/20100101 Firefox/139.0", "Firefox"),
        ("Mozilla/5.0 (iPhone) CriOS/140.0 Safari/604.1", "Chrome on iOS"),
        ("Mozilla/5.0 (iPad) FxiOS/140.0 Safari/605.1", "Firefox on iOS"),
        ("Mozilla/5.0 (iPhone) EdgiOS/140.0 Safari/605.1", "Edge on iOS"),
        ("Mozilla/5.0 (Android) EdgA/140.0 Chrome/140.0 Safari/537.36", "Edge on Android"),
        ("", "an unknown device"),
    ],
)
def test_device_label_names_browser_and_system_without_the_raw_agent(
    user_agent: str, label: str
) -> None:
    assert device_label(user_agent) == label


def test_notice_to_several_addresses_keeps_every_recipient() -> None:
    message = AccountAdapter().render_mail(
        "account/email/account_already_exists",
        ["first@example.com", "second@example.com"],
        {"email": "first@example.com", "password_reset_url": "https://testserver/app/"},
    )

    assert message.to == ["first@example.com", "second@example.com"]
    assert message.subject == "Your Pyracast account already exists"
    assert "https://testserver/app/" in str(message.body)
