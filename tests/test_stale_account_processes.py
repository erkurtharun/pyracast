import re
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from allauth.account.internal.flows.email_verification_by_code import EmailVerificationProcess
from allauth.account.internal.flows.password_reset_by_code import (
    PasswordResetVerificationProcess,
)
from allauth.account.managers import EmailAddressManager
from allauth.account.models import EmailAddress
from allauth.core.internal import ratelimit
from allauth.headless.account.inputs import VerifyEmailInput
from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, OperationalError, connection, connections
from django.db.models.signals import post_delete
from django.test import Client, override_settings

from pyracast.accounts.inputs import ResetPasswordInput
from tests.support.auth_lifecycle import AUTH, PASSWORD, member, submit
from tests.support.company_registration import signup
from tests.support.outbox import deliver_mail

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
CODE = re.compile(r"^\s*([A-Z0-9]{4}-[A-Z0-9]{4}|[0-9]{6})\s*$", re.MULTILINE)
PENDING = {"id": "verify_email", "is_pending": True}
CONFLICT = {
    "en": "The request conflicts with the current state. Reload and try again.",
    "tr": "\u0130stek mevcut durumla çak\u0131ş\u0131yor. Sayfay\u0131 yenileyip tekrar deneyin.",
}
BUSY = {
    "en": "The workspace is busy. Try again in a moment.",
    "tr": "Çal\u0131şma alan\u0131 meşgul. Biraz sonra tekrar deneyin.",
}
THROTTLED = {
    "en": "Too many requests. Wait a moment and try again.",
    "tr": "Çok fazla istek gönderildi. Biraz bekleyip tekrar deneyin.",
}


@pytest.fixture(autouse=True)
def local_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


def browser(language: str | None = None) -> Client:
    headers = {} if language is None else {"Accept-Language": language}
    return Client(enforce_csrf_checks=True, headers=headers)


def mailed_code() -> str:
    deliver_mail()
    (message,) = mail.outbox
    match = CODE.search(str(message.body))
    assert match is not None
    deliver_mail()
    mail.outbox.clear()
    return match.group(1)


def one_minute_later(monkeypatch: pytest.MonkeyPatch) -> None:
    later = time.time() + 61
    monkeypatch.setattr(
        ratelimit,
        "time",
        SimpleNamespace(time=lambda: later, monotonic=time.monotonic, sleep=time.sleep),
    )


def answer(response: Any) -> tuple[int, Any]:
    return response.status_code, response.json()


def signed_in(user: User, language: str | None = None) -> Client:
    client = browser(language)
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 200
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    return client


def pending_signup(language: str | None = None) -> tuple[Client, str]:
    client = browser(language)
    assert signup(client).status_code == 401
    return client, mailed_code()


def reset_elsewhere(email: str) -> None:
    client = browser()
    assert submit(client, "auth/password/request", {"email": email}).status_code == 401
    reset = {"key": mailed_code(), "password": PASSWORD + "reset"}
    assert submit(client, "auth/password/reset", reset).status_code == 401
    deliver_mail()
    mail.outbox.clear()


def assert_signed_out_without_pending_verification(client: Client) -> None:
    assert client.get("/api/session/").json()["user"] is None
    assert PENDING not in client.get(AUTH + "auth/session").json()["data"]["flows"]


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize(
    ("endpoint", "data"),
    [
        ("auth/email/verify", {"key": "1234-5678"}),
        ("auth/email/verify/resend", {}),
        ("auth/password/reset", {"key": "1234-5678", "password": PASSWORD + "reset"}),
    ],
)
def test_endpoints_without_a_pending_process_answer_conflict(
    endpoint: str, data: dict[str, str], authenticated: bool, language: str
) -> None:
    client = signed_in(member(), language) if authenticated else browser(language)

    response = submit(client, endpoint, data)

    conflict = {"status": 409, "code": "conflict", "message": CONFLICT[language]}
    assert answer(response) == (409, conflict)
    deliver_mail()
    assert mail.outbox == []
    assert PENDING not in client.get(AUTH + "auth/session").json()["data"].get("flows", [])


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("authenticated", [False, True])
def test_resend_rate_limit_mails_nothing_and_allows_a_later_retry(
    authenticated: bool, language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = change_pending(member(), language) if authenticated else pending_signup(language)

    response = submit(client, "auth/email/verify/resend", {})

    throttled = {"status": 429, "code": "throttled", "message": THROTTLED[language]}
    assert answer(response) == (429, throttled)
    deliver_mail()
    assert mail.outbox == []
    one_minute_later(monkeypatch)
    assert submit(client, "auth/email/verify/resend", {}).status_code == 200
    code = mailed_code()
    assert submit(client, "auth/email/verify", {"key": code}).status_code == 200


def test_signup_code_for_an_address_verified_meanwhile_answers_like_no_pending_verification() -> (
    None
):
    client, code = pending_signup()
    reset_elsewhere("owner@example.com")
    expected = answer(submit(browser(), "auth/email/verify", {"key": code}))

    response = submit(client, "auth/email/verify", {"key": code})

    assert answer(response) == expected
    deliver_mail()
    assert mail.outbox == []
    assert_signed_out_without_pending_verification(client)


def test_signup_resend_for_an_address_verified_meanwhile_mails_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = pending_signup()
    reset_elsewhere("owner@example.com")
    one_minute_later(monkeypatch)
    expected = answer(submit(browser(), "auth/email/verify/resend", {}))

    response = submit(client, "auth/email/verify/resend", {})

    assert answer(response) == expected
    deliver_mail()
    assert mail.outbox == []
    assert_signed_out_without_pending_verification(client)


def change_pending(user: User, language: str | None = None) -> tuple[Client, str]:
    client = signed_in(user, language)
    assert submit(client, "account/email", {"email": "new@example.com"}).status_code == 200
    return client, mailed_code()


def test_change_code_for_an_address_verified_out_of_band_answers_like_no_pending_change() -> None:
    user = member()
    client, code = change_pending(user)
    EmailAddress.objects.create(user=user, email="new@example.com", verified=True)
    methods = signed_in_records(client)
    expected = answer(submit(signed_in(user), "auth/email/verify", {"key": code}))

    response = submit(client, "auth/email/verify", {"key": code})

    assert answer(response) == expected
    deliver_mail()
    assert mail.outbox == []
    assert signed_in_records(client) == methods
    assert EmailAddress.objects.get(user=user, primary=True).email == "member@example.com"


def test_change_resend_for_an_address_verified_out_of_band_mails_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = member()
    client, _ = change_pending(user)
    EmailAddress.objects.create(user=user, email="new@example.com", verified=True)
    one_minute_later(monkeypatch)
    expected = answer(submit(signed_in(user), "auth/email/verify/resend", {}))

    response = submit(client, "auth/email/verify/resend", {})

    assert answer(response) == expected
    deliver_mail()
    assert mail.outbox == []


@pytest.mark.parametrize("endpoint", ["auth/email/verify", "auth/email/verify/resend"])
def test_verification_for_a_deleted_signup_account_answers_like_no_pending_verification(
    endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, code = pending_signup()
    User.objects.filter(email="owner@example.com").delete()
    one_minute_later(monkeypatch)
    expected = answer(submit(browser(), endpoint, {"key": code}))

    response = submit(client, endpoint, {"key": code})

    assert answer(response) == expected
    deliver_mail()
    assert mail.outbox == []
    assert_signed_out_without_pending_verification(client)


def test_reset_for_a_deleted_account_answers_like_no_pending_reset() -> None:
    user = member()
    client = browser()
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    reset = {"key": mailed_code(), "password": PASSWORD + "reset"}
    User.objects.filter(pk=user.pk).delete()
    expected = answer(submit(browser(), "auth/password/reset", reset))

    response = submit(client, "auth/password/reset", reset)

    assert answer(response) == expected
    deliver_mail()
    assert mail.outbox == []
    assert client.get("/api/session/").json()["user"] is None


def on_own_connection[T](work: Callable[[], T]) -> Callable[[], T]:
    def run() -> T:
        try:
            return work()
        finally:
            connections.close_all()

    return run


def elsewhere(work: Callable[[], object]) -> None:
    with ThreadPoolExecutor(1) as pool:
        pool.submit(on_own_connection(work)).result(timeout=10)


def before(
    monkeypatch: pytest.MonkeyPatch, owner: type, name: str, competitor: Callable[[], object]
) -> None:
    original = getattr(owner, name)

    def run_competitor_first(self: Any, *args: Any, **kwargs: Any) -> Any:
        elsewhere(competitor)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(owner, name, run_competitor_first)


def verified_signup_address() -> None:
    EmailAddress.objects.filter(email="owner@example.com").update(verified=True)


def verified_change_address(user: User) -> Callable[[], object]:
    return lambda: EmailAddress.objects.get_or_create(
        user=user, email="new@example.com", defaults={"verified": True}
    )


def after(
    monkeypatch: pytest.MonkeyPatch, owner: type, name: str, competitor: Callable[[], object]
) -> None:
    original = getattr(owner, name)

    def run_competitor_next(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        elsewhere(competitor)
        return result

    monkeypatch.setattr(owner, name, run_competitor_next)


def before_property(
    monkeypatch: pytest.MonkeyPatch, owner: type, name: str, competitor: Callable[[], object]
) -> None:
    original = getattr(owner, name)

    def run_competitor_first(self: Any) -> Any:
        elsewhere(competitor)
        return original.fget(self)

    monkeypatch.setattr(owner, name, property(run_competitor_first))


def signed_in_records(client: Client) -> list[dict[str, Any]]:
    # The session's authentication records without the reauthentication flag, which ends when the
    # account's credential state changes; the records themselves stay with the sign-in.
    records = client.get(AUTH + "auth/session").json()["data"]["methods"]
    return [{k: v for k, v in record.items() if k != "reauthenticated"} for record in records]


def test_signup_code_answers_like_nothing_pending_when_verified_before_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, code = pending_signup()
    expected = answer(submit(browser(), "auth/email/verify", {"key": code}))
    after(monkeypatch, VerifyEmailInput, "clean_key", verified_signup_address)

    response = submit(client, "auth/email/verify", {"key": code})

    assert answer(response) == expected
    deliver_mail()
    assert mail.outbox == []
    assert_signed_out_without_pending_verification(client)
    assert EmailAddress.objects.get(email="owner@example.com").verified


def test_change_code_answers_like_nothing_pending_when_verified_before_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = member()
    client, code = change_pending(user)
    methods = signed_in_records(client)
    expected = answer(submit(signed_in(user), "auth/email/verify", {"key": code}))
    after(monkeypatch, VerifyEmailInput, "clean_key", verified_change_address(user))

    response = submit(client, "auth/email/verify", {"key": code})

    assert answer(response) == expected
    deliver_mail()
    assert mail.outbox == []
    assert signed_in_records(client) == methods
    assert EmailAddress.objects.get(user=user, email="new@example.com").verified
    assert EmailAddress.objects.get(user=user, primary=True).email == "member@example.com"


def test_signup_resend_mails_nothing_when_verified_before_the_code_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = pending_signup()
    one_minute_later(monkeypatch)
    expected = answer(submit(browser(), "auth/email/verify/resend", {}))
    before_property(monkeypatch, EmailVerificationProcess, "can_resend", verified_signup_address)

    response = submit(client, "auth/email/verify/resend", {})

    assert answer(response) == expected
    deliver_mail()
    assert mail.outbox == []
    assert_signed_out_without_pending_verification(client)
    assert EmailAddress.objects.get(email="owner@example.com").verified


def test_change_resend_mails_nothing_when_verified_before_the_code_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = member()
    client, _ = change_pending(user)
    methods = signed_in_records(client)
    one_minute_later(monkeypatch)
    expected = answer(submit(signed_in(user), "auth/email/verify/resend", {}))
    before_property(
        monkeypatch, EmailVerificationProcess, "can_resend", verified_change_address(user)
    )

    response = submit(client, "auth/email/verify/resend", {})

    assert answer(response) == expected
    deliver_mail()
    assert mail.outbox == []
    assert signed_in_records(client) == methods
    assert EmailAddress.objects.get(user=user, email="new@example.com").verified


def test_reset_answers_like_nothing_pending_when_the_account_is_deleted_before_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = member()
    client = browser()
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    reset = {"key": mailed_code(), "password": PASSWORD + "reset"}
    expected = answer(submit(browser(), "auth/password/reset", reset))
    before(
        monkeypatch,
        ResetPasswordInput,
        "clean",
        lambda: User.objects.filter(pk=user.pk).delete(),
    )

    response = submit(client, "auth/password/reset", reset)

    assert answer(response) == expected
    deliver_mail()
    assert mail.outbox == []
    assert not User.objects.filter(pk=user.pk).exists()
    assert client.get("/api/session/").json()["user"] is None


def account(email: str, username: str, *, verified: bool = True) -> User:
    user = User.objects.create_user(username=username, email=email, password=PASSWORD)
    EmailAddress.objects.create(user=user, email=email, primary=True, verified=verified)
    return user


def code_to(address: str) -> str:
    deliver_mail()
    (message,) = [message for message in mail.outbox if message.to == [address]]
    match = CODE.search(str(message.body))
    assert match is not None
    return match.group(1)


def sent() -> list[tuple[str, list[str]]]:
    deliver_mail()
    return [(message.extra_headers["X-Pyracast-Mail"], message.to) for message in mail.outbox]


def addresses(user: User) -> list[tuple[str, bool, bool]]:
    return list(EmailAddress.objects.filter(user=user).values_list("email", "primary", "verified"))


def verify(client: Client, key: str) -> Any:
    return submit(client, "auth/email/verify", {"key": key})


def test_accounts_verifying_each_others_stored_address_both_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = account("first@example.com", "first")
    second = account("second@example.com", "second")
    EmailAddress.objects.create(user=first, email="x@example.com")
    EmailAddress.objects.create(user=second, email="y@example.com")
    changes = [(signed_in(first), "y@example.com"), (signed_in(second), "x@example.com")]
    for client, address in changes:
        assert submit(client, "account/email", {"email": address}).status_code == 200
    keys = [code_to(address) for _, address in changes]
    deliver_mail()
    mail.outbox.clear()
    together = threading.Barrier(2, timeout=10)
    finish = EmailVerificationProcess.finish

    def finish_together(self: EmailVerificationProcess) -> EmailAddress | None:
        together.wait()
        return finish(self)

    monkeypatch.setattr(EmailVerificationProcess, "finish", finish_together)

    with ThreadPoolExecutor(2) as pool:
        verifications = [
            pool.submit(on_own_connection(partial(verify, client, key)))
            for (client, _), key in zip(changes, keys, strict=True)
        ]
        statuses = [verification.result(timeout=30).status_code for verification in verifications]

    assert statuses == [200, 200]
    assert addresses(first) == [("y@example.com", True, True)]
    assert addresses(second) == [("x@example.com", True, True)]


deleting = threading.local()


def blocked_by(pid: int) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))", [pid]
        )
        row = cursor.fetchone()
    return bool(row and row[0])


def during_deletion(user: User, request: Callable[[], Any]) -> Any:
    addresses_gone = threading.Event()
    resume = threading.Event()
    deleter_pid: list[int] = []

    def pause(**kwargs: Any) -> None:
        if getattr(deleting, "active", False) and not addresses_gone.is_set():
            addresses_gone.set()
            assert resume.wait(timeout=10)

    def delete() -> None:
        deleting.active = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            deleter_pid.extend(cursor.fetchone() or ())
        user.delete()

    post_delete.connect(pause, sender=EmailAddress, weak=False)
    try:
        with ThreadPoolExecutor(1) as deleter, ThreadPoolExecutor(1) as requester:
            deletion = deleter.submit(on_own_connection(delete))
            assert addresses_gone.wait(timeout=10)
            response: Future[Any] = requester.submit(on_own_connection(request))
            deadline = time.monotonic() + 10
            while not response.done() and not blocked_by(deleter_pid[0]):
                assert time.monotonic() < deadline
                time.sleep(0.02)
            resume.set()
            deletion.result(timeout=20)
            return response.result(timeout=20)
    finally:
        post_delete.disconnect(pause, sender=EmailAddress)


def test_verification_during_account_deletion_answers_like_a_serial_order() -> None:
    client, code = pending_signup()
    owner = User.objects.get(email="owner@example.com")

    response = during_deletion(owner, lambda: verify(client, code))

    assert (response.status_code, sent()) in [(409, []), (200, [])]
    assert not User.objects.filter(pk=owner.pk).exists()
    assert client.get("/api/session/").json()["user"] is None


def test_resend_during_account_deletion_answers_like_a_serial_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = pending_signup()
    owner = User.objects.get(email="owner@example.com")
    one_minute_later(monkeypatch)

    response = during_deletion(owner, lambda: submit(client, "auth/email/verify/resend", {}))

    signup_code = ("account/email/email_confirmation_signup", ["owner@example.com"])
    assert (response.status_code, sent()) in [(409, []), (200, [signup_code])]
    assert not User.objects.filter(pk=owner.pk).exists()


def pending_unverified_reset() -> tuple[User, Client, dict[str, str]]:
    owner = account("unverified@example.com", "unverified", verified=False)
    client = browser()
    assert submit(client, "auth/password/request", {"email": owner.email}).status_code == 401
    return owner, client, {"key": mailed_code(), "password": PASSWORD + "reset"}


def test_reset_during_account_deletion_answers_like_a_serial_order() -> None:
    owner, client, reset = pending_unverified_reset()

    response = during_deletion(owner, lambda: submit(client, "auth/password/reset", reset))

    notice = ("account/email/password_reset", ["unverified@example.com"])
    assert (response.status_code, sent()) in [(409, []), (401, [notice])]
    assert not User.objects.filter(pk=owner.pk).exists()


def test_verification_never_writes_over_an_address_reassigned_before_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, code = pending_signup()
    other = member()
    after(
        monkeypatch,
        VerifyEmailInput,
        "clean_key",
        lambda: EmailAddress.objects.filter(email="owner@example.com").update(
            user=other, primary=False, verified=True
        ),
    )

    response = verify(client, code)

    assert answer(response) == answer(verify(browser(), code))
    deliver_mail()
    assert mail.outbox == []
    assert_signed_out_without_pending_verification(client)
    assert EmailAddress.objects.filter(
        email="owner@example.com", user=other, verified=True
    ).exists()


def test_verification_never_writes_back_an_address_edited_before_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, code = pending_signup()
    after(
        monkeypatch,
        VerifyEmailInput,
        "clean_key",
        lambda: EmailAddress.objects.filter(email="owner@example.com").update(
            email="edited@example.com"
        ),
    )

    response = verify(client, code)

    assert answer(response) == answer(verify(browser(), code))
    deliver_mail()
    assert mail.outbox == []
    assert_signed_out_without_pending_verification(client)
    rows = EmailAddress.objects.values_list("email", "primary", "verified")
    assert list(rows) == [("edited@example.com", True, False)]


def test_change_answers_conflict_when_the_address_is_stored_before_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = member()
    client, code = change_pending(user)
    expected = answer(submit(signed_in(user), "auth/email/verify", {"key": code}))
    before(
        monkeypatch,
        EmailVerificationProcess,
        "finish",
        lambda: EmailAddress.objects.create(user=user, email="new@example.com"),
    )

    response = submit(client, "auth/email/verify", {"key": code})

    assert answer(response) == expected
    deliver_mail()
    assert mail.outbox == []
    stored = [("member@example.com", True, True), ("new@example.com", False, False)]
    assert sorted(addresses(user)) == stored
    assert answer(submit(client, "auth/email/verify", {"key": code})) == expected


def verified_reset_address() -> None:
    EmailAddress.objects.filter(email="unverified@example.com").update(verified=True)


def reassigned_reset_address() -> None:
    EmailAddress.objects.filter(email="unverified@example.com").update(
        user=User.objects.get(username="member"), primary=False, verified=True
    )


@pytest.mark.parametrize(
    ("competitor", "holder"),
    [(verified_reset_address, "unverified"), (reassigned_reset_address, "member")],
    ids=["verified", "reassigned"],
)
def test_reset_never_writes_over_an_address_changed_before_completion(
    competitor: Callable[[], None], holder: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    member()
    owner, client, reset = pending_unverified_reset()
    nothing_pending = answer(submit(browser(), "auth/password/reset", reset))
    after(monkeypatch, EmailAddressManager, "get_for_user", competitor)

    response = submit(client, "auth/password/reset", reset)

    assert answer(response) == nothing_pending
    deliver_mail()
    assert mail.outbox == []
    owner.refresh_from_db()
    assert owner.check_password(PASSWORD)
    address = EmailAddress.objects.select_related("user").get(email="unverified@example.com")
    assert (address.user.username, address.verified) == (holder, True)
    assert answer(submit(client, "auth/password/reset", reset)) == nothing_pending


def test_reset_of_an_address_replaced_before_completion_answers_like_nothing_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, client, reset = pending_unverified_reset()
    nothing_pending = answer(submit(browser(), "auth/password/reset", reset))

    def replaced() -> None:
        EmailAddress.objects.filter(user=owner).delete()
        EmailAddress.objects.create(
            user=owner, email="changed@example.com", primary=True, verified=True
        )

    before(monkeypatch, ResetPasswordInput, "clean", replaced)

    response = submit(client, "auth/password/reset", reset)

    assert answer(response) == nothing_pending
    assert sent() == []
    owner.refresh_from_db()
    assert owner.check_password(PASSWORD)
    assert addresses(owner) == [("changed@example.com", True, True)]


def deadlock() -> None:
    error = OperationalError("deadlock detected")
    error.__cause__ = psycopg.errors.DeadlockDetected("deadlock detected")
    raise error


def missing_row() -> None:
    raise ObjectDoesNotExist


def missing_account() -> None:
    raise User.NotUpdated("Save with update_fields did not affect any rows.")


def verified_elsewhere() -> None:
    other = User.objects.create_user(username="other", email="other@example.com")
    address = EmailAddress.objects.filter(verified=True).exclude(user=other).get()
    EmailAddress.objects.create(user=other, email=address.email, verified=True)


def second_primary() -> None:
    address = EmailAddress.objects.get(primary=True)
    EmailAddress.objects.create(user=address.user, email="unexpected@example.com", primary=True)


def lost_connection() -> None:
    raise OperationalError("server closed the connection")


def lock_timeout() -> None:
    error = OperationalError("canceling statement due to lock timeout")
    error.__cause__ = psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")
    raise error


FAILURES: dict[str, Callable[[], None]] = {
    "unique-rule": verified_elsewhere,
    "row-gone": missing_row,
    "account-gone": missing_account,
    "deadlock": deadlock,
}


def fail_after(monkeypatch: pytest.MonkeyPatch, owner: type, failure: Callable[[], None]) -> None:
    original = owner.finish  # type: ignore[attr-defined]

    def finish_then_fail(self: Any) -> Any:
        result = original(self)
        failure()
        return result

    monkeypatch.setattr(owner, "finish", finish_then_fail)


@pytest.mark.parametrize("failure", FAILURES.values(), ids=FAILURES)
def test_failed_verification_writes_nothing_and_discards_the_process(
    failure: Callable[[], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, code = pending_signup()
    fail_after(monkeypatch, EmailVerificationProcess, failure)

    response = verify(client, code)

    assert answer(response) == answer(verify(browser(), code))
    deliver_mail()
    assert mail.outbox == []
    assert not EmailAddress.objects.get(email="owner@example.com").verified
    assert_signed_out_without_pending_verification(client)


@pytest.mark.parametrize("failure", FAILURES.values(), ids=FAILURES)
def test_failed_reset_writes_nothing_and_discards_the_process(
    failure: Callable[[], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    user = member()
    client = browser()
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    reset = {"key": mailed_code(), "password": PASSWORD + "reset"}
    fail_after(monkeypatch, PasswordResetVerificationProcess, failure)

    response = submit(client, "auth/password/reset", reset)

    nothing_pending = answer(submit(browser(), "auth/password/reset", reset))
    assert answer(response) == nothing_pending
    deliver_mail()
    assert mail.outbox == []
    user.refresh_from_db()
    assert user.check_password(PASSWORD)
    assert answer(submit(client, "auth/password/reset", reset)) == nothing_pending


def pending_completion(
    owner: type, language: str | None = None
) -> tuple[Client, str, dict[str, str]]:
    if owner is EmailVerificationProcess:
        client, key = pending_signup(language)
        return client, "auth/email/verify", {"key": key}
    user = member()
    client = browser(language)
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    return client, "auth/password/reset", {"key": mailed_code(), "password": PASSWORD + "reset"}


@pytest.mark.parametrize(
    ("failure", "error"),
    [(lost_connection, OperationalError), (second_primary, IntegrityError)],
    ids=["lost-connection", "other-constraint"],
)
@pytest.mark.parametrize("owner", [EmailVerificationProcess, PasswordResetVerificationProcess])
def test_completion_does_not_answer_other_database_errors_as_conflicts(
    owner: type,
    failure: Callable[[], None],
    error: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, endpoint, data = pending_completion(owner)
    fail_after(monkeypatch, owner, failure)

    with pytest.raises(error):
        submit(client, endpoint, data)


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("owner", [EmailVerificationProcess, PasswordResetVerificationProcess])
def test_completion_past_the_lock_timeout_answers_busy(
    owner: type, language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, endpoint, data = pending_completion(owner, language)
    client.raise_request_exception = False
    fail_after(monkeypatch, owner, lock_timeout)

    response = submit(client, endpoint, data)

    busy = {"code": "busy", "message": BUSY[language], "retry_after": 1}
    assert answer(response) == (503, busy)
    assert response["Retry-After"] == "1"
    deliver_mail()
    assert mail.outbox == []
