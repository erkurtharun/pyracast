import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from typing import Any

import pytest
from allauth.account.internal.flows.email_verification_by_code import EmailVerificationProcess
from allauth.core.internal import ratelimit
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.usersessions.models import UserSession
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sessions.backends.db import SessionStore
from django.core import mail
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from pyracast.accounts import middleware as account_middleware
from pyracast.accounts.inputs import ReauthenticateInput
from pyracast.accounts.language import save_user_language
from pyracast.accounts.models import UserLanguagePreference
from pyracast.accounts.session_cookie import SessionCookie
from pyracast.languages import Language
from tests.support.account_sessions import (
    account,
    alongside,
    browser,
    capture_security_events,
    elsewhere,
    moment,
    session_key,
    session_user,
    signed_in,
    totp_clock,
    with_totp,
)
from tests.support.auth_lifecycle import AUTH, PASSWORD, mailed_code, submit, totp_code
from tests.support.outbox import deliver_mail

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


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


def revocations(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in records if record["event"] == "sessions_revoked"]


def activate_totp(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    totp_clock(monkeypatch, now)
    secret = client.get(AUTH + "account/authenticators/totp").json()["meta"]["secret"]
    activation = {"code": totp_code(secret, now)}
    assert submit(client, "account/authenticators/totp", activation).status_code == 200


def with_mfa(user: User, secret: str, monkeypatch: pytest.MonkeyPatch, now: float) -> Client:
    totp_clock(monkeypatch, now)
    client = browser()
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 401
    code = {"code": totp_code(secret, now)}
    assert submit(client, "auth/2fa/authenticate", code).status_code == 200
    later = now + 31
    totp_clock(monkeypatch, later)
    code = {"code": totp_code(secret, later)}
    assert submit(client, "auth/2fa/reauthenticate", code).status_code == 200
    return client


def regenerate_recovery_codes(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    assert submit(client, "account/authenticators/recovery-codes", {}).status_code == 200


def change_address(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    assert submit(client, "account/email", {"email": "changed@example.com"}).status_code == 200
    assert submit(client, "auth/email/verify", {"key": mailed_code()}).status_code == 200


@pytest.mark.parametrize(
    ("change", "trigger"),
    [
        (activate_totp, "mfa_added"),
        (regenerate_recovery_codes, "mfa_reset"),
        (change_address, "email_changed"),
    ],
)
def test_a_credential_change_ends_the_other_sessions_of_the_account(
    change: Callable[[Client, pytest.MonkeyPatch], None],
    trigger: str,
    monkeypatch: pytest.MonkeyPatch,
    events: Callable[[], list[dict[str, Any]]],
) -> None:
    user = account("owner")
    if change is regenerate_recovery_codes:
        secret = with_totp(user)
        RecoveryCodes.activate(user)
        other = with_mfa(user, secret, monkeypatch, time.time())
        current = with_mfa(user, secret, monkeypatch, time.time() + 62)
    else:
        other = signed_in(user)
        current = signed_in(user, reauthenticated=True)
    unrelated = signed_in(account("unrelated"))

    change(current, monkeypatch)

    assert session_user(other) is None
    assert session_user(current) == user.pk
    assert session_user(unrelated) is not None
    assert [session_key(current)] == list(
        UserSession.objects.filter(user=user).values_list("session_key", flat=True)
    )
    expected = {"event": "sessions_revoked", "user_id": user.pk, "reason": trigger, "count": 1}
    records = revocations(events())
    assert [{key: record.get(key) for key in expected} for record in records] == [expected]


def test_a_password_reset_ends_every_session_of_the_account(
    events: Callable[[], list[dict[str, Any]]],
) -> None:
    user = account("owner")
    sessions = [signed_in(user), signed_in(user)]
    reset = browser()
    assert submit(reset, "auth/password/request", {"email": user.email}).status_code == 401
    data = {"key": mailed_code(), "password": PASSWORD + "reset"}

    assert submit(reset, "auth/password/reset", data).status_code == 401

    assert [session_user(client) for client in sessions] == [None, None]
    assert not UserSession.objects.filter(user=user).exists()
    (revoked,) = revocations(events())
    assert (revoked["reason"], revoked["count"]) == ("password_reset", 2)


def test_a_password_change_ends_every_session_of_the_account() -> None:
    user = account("owner")
    other = signed_in(user)
    current = signed_in(user)
    change = {"current_password": PASSWORD, "new_password": PASSWORD + "changed"}

    assert submit(current, "account/password/change", change).status_code == 401

    assert [session_user(other), session_user(current)] == [None, None]
    assert not UserSession.objects.filter(user=user).exists()


def test_signing_out_removes_the_device_record_of_the_session() -> None:
    user = account("owner")
    client = signed_in(user)
    key = session_key(client)
    token = client.get("/api/session/").json()["csrf_token"]

    assert client.delete(AUTH + "auth/session", HTTP_X_CSRFTOKEN=token).status_code == 401

    assert not UserSession.objects.filter(session_key=key).exists()


@pytest.mark.parametrize(
    ("marker", "limit"),
    [("last_activity_at", 30 * 60), ("authenticated_at", 12 * 60 * 60)],
)
@pytest.mark.parametrize(("offset", "kept"), [(-1, True), (0, False)])
def test_a_session_ends_exactly_at_its_idle_and_absolute_limits(
    marker: str, limit: int, offset: int, kept: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = account("owner")
    client = signed_in(user)
    started = time.time()
    now = started + limit + offset
    session = client.session
    session["authenticated_at"] = started
    session["last_activity_at"] = started if marker == "last_activity_at" else now - 30
    session.save()
    monkeypatch.setattr(time, "time", lambda: now)

    assert (session_user(client) == user.pk) is kept


def test_session_activity_is_written_at_most_once_a_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = signed_in(account("owner"))
    started = time.time()

    def writes(at: float) -> int:
        monkeypatch.setattr(time, "time", lambda: at)
        with CaptureQueriesContext(connection) as queries:
            assert client.get("/api/session/").status_code == 200
        return sum(
            1
            for query in queries.captured_queries
            if "usersessions_usersession" in query["sql"]
            and query["sql"].lstrip().upper().startswith(("UPDATE", "INSERT"))
        )

    elapsed = [1, 30, 61, 90, 122]
    assert [writes(started + seconds) for seconds in elapsed] == [0, 0, 1, 0, 1]


def test_reauthentication_rotates_the_session_key() -> None:
    user = account("owner")
    client = signed_in(user)
    before = session_key(client)
    stale = browser()
    stale.cookies[settings.SESSION_COOKIE_NAME] = before

    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200

    after = session_key(client)
    assert after != before
    assert session_user(client) == user.pk
    assert session_user(stale) is None
    devices = UserSession.objects.filter(user=user).values_list("session_key", flat=True)
    assert list(devices) == [after]


def confirm_change(client: Client, code_after: threading.Event | None = None) -> None:
    assert submit(client, "account/email", {"email": "changed@example.com"}).status_code == 200
    if code_after is not None:
        assert code_after.wait(timeout=10)
    assert submit(client, "auth/email/verify", {"key": mailed_code()}).status_code == 200


def changed_during_the_password_check(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[], object]
) -> list[tuple[bool, Future[None]]]:
    clean: Callable[..., Any] = ReauthenticateInput.clean
    changes: list[tuple[bool, Future[None]]] = []

    def changed_meanwhile(self: ReauthenticateInput) -> Any:
        cleaned = clean(self)
        future = alongside(change)
        changes.append((future.done(), future))
        return cleaned

    monkeypatch.setattr(ReauthenticateInput, "clean", changed_meanwhile)
    return changes


def test_a_change_that_ends_a_session_waits_for_the_reauthentication_it_meets_and_ends_it_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = account("owner")
    rotating = signed_in(user)
    other = signed_in(user, reauthenticated=True)
    answered = threading.Event()
    changes = changed_during_the_password_check(
        monkeypatch, lambda: confirm_change(other, code_after=answered)
    )

    response = submit(rotating, "auth/reauthenticate", {"password": PASSWORD})
    answered.set()
    [(finished, change)] = changes
    change.result(timeout=10)

    assert not finished
    assert response.status_code == 200
    assert session_user(rotating) is None
    assert session_user(other) == user.pk
    devices = UserSession.objects.filter(user=user).values_list("session_key", flat=True)
    assert list(devices) == [session_key(other)]


def test_a_change_that_ends_a_reauthenticated_session_before_its_answer_is_saved_ends_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = account("owner")
    rotating = signed_in(user)
    other = signed_in(user, reauthenticated=True)
    changes = changed_during_the_password_check(monkeypatch, lambda: confirm_change(other))
    process_response = SessionCookie.process_response
    answering = threading.get_ident()

    def saved_after_the_change(self: SessionCookie, request: Any, response: Any) -> Any:
        if threading.get_ident() == answering and changes:
            changes[0][1].result(timeout=10)
        return process_response(self, request, response)

    monkeypatch.setattr(SessionCookie, "process_response", saved_after_the_change)

    response = submit(rotating, "auth/reauthenticate", {"password": PASSWORD})
    [(finished, change)] = changes
    change.result(timeout=10)

    assert not finished
    assert (response.status_code, response.json()["code"]) == (400, "session_ended")
    assert session_user(rotating) is None
    assert session_user(other) == user.pk
    devices = UserSession.objects.filter(user=user).values_list("session_key", flat=True)
    assert list(devices) == [session_key(other)]


def test_a_session_rotating_while_another_session_ends_it_stays_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = account("owner")
    rotating = signed_in(user)
    other = signed_in(user, reauthenticated=True)
    cycle_key = SessionStore.cycle_key
    revocations: list[Future[None]] = []

    def revoked_meanwhile(self: SessionStore) -> None:
        revocations.append(alongside(lambda: confirm_change(other)))
        cycle_key(self)

    monkeypatch.setattr(SessionStore, "cycle_key", revoked_meanwhile)

    submit(rotating, "auth/reauthenticate", {"password": PASSWORD})
    revocations[0].result(timeout=10)

    assert session_user(rotating) is None
    assert session_user(other) == user.pk
    devices = UserSession.objects.filter(user=user).values_list("session_key", flat=True)
    assert list(devices) == [session_key(other)]


def test_the_session_middleware_order_is_checked() -> None:
    from pyracast.accounts.checks import check_session_middleware_order

    assert check_session_middleware_order() == []
    swapped = list(settings.MIDDLEWARE)
    first = swapped.index("pyracast.accounts.session_cookie.SessionCookie")
    second = swapped.index("django.contrib.auth.middleware.AuthenticationMiddleware")
    swapped[first], swapped[second] = swapped[second], swapped[first]
    missing = [name for name in settings.MIDDLEWARE if "SessionLifetime" not in name]

    for middleware in (swapped, missing):
        with override_settings(MIDDLEWARE=middleware):
            assert [error.id for error in check_session_middleware_order()] == ["accounts.E003"]


def test_a_request_whose_session_was_ended_meanwhile_answers_session_ended_after_its_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = account("owner")
    working = signed_in(user)
    competitor = signed_in(user, reauthenticated=True)
    assert submit(competitor, "account/email", {"email": "other@example.com"}).status_code == 200
    competitor_code = mailed_code()
    token = working.get("/api/session/").json()["csrf_token"]

    def ended_meanwhile(user: User, language: Language) -> None:
        elsewhere(lambda: submit(competitor, "auth/email/verify", {"key": competitor_code}))
        save_user_language(user, language)

    monkeypatch.setattr("pyracast.api.session.save_user_language", ended_meanwhile)
    # The request records its activity, so it writes its session.
    monkeypatch.setattr(account_middleware, "time", moment(time.time() + 61))

    response = working.post(
        "/api/session/language/",
        {"language": "tr"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )

    assert (response.status_code, response.json()["code"]) == (400, "session_ended")
    assert UserLanguagePreference.objects.get(user=user).language == "tr"
    assert session_user(working) is None
    assert session_user(competitor) == user.pk


def test_a_resend_whose_account_another_session_changed_meanwhile_sends_no_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = account("owner")
    pending = signed_in(user, reauthenticated=True)
    assert submit(pending, "account/email", {"email": "pending@example.com"}).status_code == 200
    competitor = signed_in(user, reauthenticated=True)
    assert submit(competitor, "account/email", {"email": "other@example.com"}).status_code == 200
    competitor_code = mailed_code()
    deliver_mail()
    mail.outbox.clear()
    monkeypatch.setattr(ratelimit, "time", moment(time.time() + 61))
    resend = EmailVerificationProcess.resend

    def ended_meanwhile(self: EmailVerificationProcess) -> None:
        resend(self)
        elsewhere(lambda: submit(competitor, "auth/email/verify", {"key": competitor_code}))

    monkeypatch.setattr(EmailVerificationProcess, "resend", ended_meanwhile)

    response = submit(pending, "auth/email/verify/resend", {})

    assert (response.status_code, response.json()["code"]) == (400, "session_ended")
    deliver_mail()
    recipients = sorted(message.to[0] for message in mail.outbox)
    assert recipients == ["owner@example.com"]
    assert session_user(pending) is None
    assert session_user(competitor) == user.pk
