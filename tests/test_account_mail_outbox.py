import re
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import Any, ClassVar

import pytest
from allauth.account.models import EmailAddress
from allauth.core.internal import ratelimit
from django.contrib.auth.models import User
from django.core.mail import EmailMessage
from django.core.mail.backends.base import BaseEmailBackend
from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from django.test import Client, override_settings
from django.utils import timezone
from psycopg import sql

from pyracast.accounts.cache import AuthenticationCache
from pyracast.mail.errors import PermanentlyNotSentError
from tests.support.auth_lifecycle import (
    LONGEST_ACCOUNT_ADDRESS,
    LONGEST_ADDRESS,
    OVERLONG_ACCOUNT_ADDRESS,
    PASSWORD,
    Clock,
    member,
    submit,
)
from tests.support.company_registration import signup
from tests.support.outbox import after_the_lease, deliver_mail

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
RESEND = "auth/email/verify/resend"


class WorkerCrash(BaseException):
    pass


class Transport(BaseEmailBackend):
    sent: ClassVar[list[EmailMessage]] = []
    refusal: ClassVar[Exception | None] = None
    crash_after_acceptance: ClassVar[bool] = False
    failure_after_acceptance: ClassVar[Exception | None] = None

    def send_messages(self, email_messages: Sequence[EmailMessage] | None) -> int:
        assert email_messages is not None
        if self.refusal is not None:
            raise self.refusal
        self.sent.extend(email_messages)
        if self.crash_after_acceptance:
            raise WorkerCrash
        if self.failure_after_acceptance is not None:
            raise self.failure_after_acceptance
        return len(email_messages)


@pytest.fixture(autouse=True)
def transport() -> Iterator[type[Transport]]:
    Transport.sent = []
    Transport.refusal = None
    Transport.crash_after_acceptance = False
    Transport.failure_after_acceptance = None
    with override_settings(
        MAILERS={"default": {"BACKEND": "tests.test_account_mail_outbox.Transport"}}
    ):
        yield Transport


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock(time.time())
    monkeypatch.setattr(ratelimit, "time", clock.reading())
    return clock


def visitor(index: int) -> Client:
    return Client(enforce_csrf_checks=True, REMOTE_ADDR=f"198.51.100.{index + 1}")


def request_reset(index: int, email: str) -> Any:
    return submit(visitor(index), "auth/password/request", {"email": email})


def intents() -> list[tuple[str, str]]:
    from pyracast.accounts.models import OutboundMail

    return list(OutboundMail.objects.order_by("pk").values_list("kind", "recipient"))


def mail_row_holds(text: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                "SELECT count(*) FROM {} AS stored WHERE strpos(row_to_json(stored)::text, %s) > 0"
            ).format(sql.Identifier("notifications_outboundmail")),
            [text],
        )
        return bool(cursor.fetchone() != (0,))


def test_a_password_reset_request_answers_alike_and_mails_only_from_the_worker(
    transport: type[Transport],
) -> None:
    user = member()

    known = request_reset(0, user.email)
    unknown = request_reset(1, "absent@example.com")

    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json())
    assert transport.sent == []
    assert intents() == [
        ("account/email/password_reset_code", "member@example.com"),
        ("account/email/unknown_account", "absent@example.com"),
    ]
    assert deliver_mail() == 2
    assert [
        (message.to, message.extra_headers["X-Pyracast-Mail"]) for message in transport.sent
    ] == [
        (["member@example.com"], "account/email/password_reset_code"),
        (["absent@example.com"], "account/email/unknown_account"),
    ]


def test_every_account_notice_is_a_kind_of_outbound_mail() -> None:
    from pyracast.accounts.account_mail import NOTICES
    from pyracast.accounts.models import OutboundMailKind

    assert set(NOTICES) - set(OutboundMailKind.values) == set()


def test_a_mailed_code_rests_only_sealed_and_is_erased_once_sent(
    transport: type[Transport],
) -> None:
    from pyracast.accounts.models import OutboundMail
    from pyracast.notifications.outbox import unseal_code

    user = member()
    request_reset(0, user.email)
    row = OutboundMail.objects.get()
    code = unseal_code(row.sealed_code)

    assert code
    assert code not in row.sealed_code
    assert not mail_row_holds(code)
    assert deliver_mail() == 1
    assert code in str(transport.sent[0].body)
    row.refresh_from_db()
    assert (row.status, row.sealed_code) == ("sent", "")


def mailed_code(message: EmailMessage) -> str:
    codes: list[str] = re.findall(r"^\s*([A-Z0-9]{4}-[A-Z0-9]{4})\s*$", str(message.body), re.M)
    (code,) = codes
    return code


def test_an_uncertain_code_mail_holds_no_code_and_a_new_code_is_mailed_at_once(
    transport: type[Transport],
) -> None:
    from pyracast.accounts.models import OutboundMail

    user = member()
    client = visitor(0)
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    transport.crash_after_acceptance = True
    with pytest.raises(WorkerCrash):
        deliver_mail()
    transport.crash_after_acceptance = False
    assert deliver_mail(after_the_lease()) == 0
    lost = mailed_code(transport.sent[0])

    assert list(OutboundMail.objects.values_list("status", "sealed_code")) == [("uncertain", "")]
    assert not mail_row_holds(lost)
    assert submit(client, "auth/password/request", {"email": user.email}).status_code == 401
    assert deliver_mail() == 1
    code = mailed_code(transport.sent[-1])
    assert code != lost
    password = PASSWORD + "-renewed"
    reset = submit(client, "auth/password/reset", {"key": code, "password": password})
    assert reset.status_code == 401
    assert (
        submit(visitor(1), "auth/login", {"email": user.email, "password": password}).status_code
        == 200
    )


def end_mails(transport: type[Transport], outcome: str) -> None:
    """Ends every due intent: `failed` and `uncertain` through the transport, `pending` leaves
    them waiting, any other outcome lets the worker decide as usual."""
    if outcome == "pending":
        return
    if outcome == "failed":
        transport.refusal = PermanentlyNotSentError("relay refused the message")
    elif outcome == "uncertain":
        transport.failure_after_acceptance = TimeoutError("relay answer lost")
    deliver_mail()
    transport.refusal = transport.failure_after_acceptance = None


@pytest.mark.parametrize("outcome", ["failed", "uncertain"])
def test_a_verification_code_whose_mail_failed_or_ended_uncertain_is_asked_again_after_a_minute(
    transport: type[Transport], clock: Clock, outcome: str
) -> None:
    from pyracast.accounts.models import OutboundMail

    client = Client(enforce_csrf_checks=True)
    assert signup(client).status_code == 401
    end_mails(transport, outcome)
    waiting = OutboundMail.objects.filter(status="pending")

    clock.advance(59)
    assert submit(client, RESEND, {}).status_code == 429
    assert not waiting.exists()
    clock.advance(2)
    assert submit(client, RESEND, {}).status_code == 200
    assert list(waiting.values_list("kind", flat=True)) == [
        "account/email/email_confirmation_signup"
    ]
    assert deliver_mail() == 1
    code = mailed_code(transport.sent[-1])
    assert code not in [mailed_code(message) for message in transport.sent[:-1]]
    assert submit(client, "auth/email/verify", {"key": code}).status_code == 200


def pending_signup(index: int, address: str) -> Client:
    client = visitor(index)
    details = {"company_name": f"Company {index}", "email": address}
    assert signup(client, details).status_code == 401
    return client


def reauthenticated_changer(index: int) -> Client:
    user = User.objects.create_user(
        username=f"changer-{index}", email=f"changer-{index}@example.com", password=PASSWORD
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    client = visitor(index)
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 200
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    return client


def pending_address_change(index: int, address: str) -> Client:
    client = reauthenticated_changer(index)
    assert submit(client, "account/email", {"email": address}).status_code == 200
    return client


def registered(address: str) -> User:
    user = User.objects.create_user(username="taken", email=address, password=PASSWORD)
    EmailAddress.objects.create(user=user, email=address, primary=True, verified=True)
    return user


def spend_anonymous_budget() -> None:
    from pyracast.notifications.outbox import ANONYMOUS_MAIL_PER_HOUR

    for index in range(ANONYMOUS_MAIL_PER_HOUR):
        assert request_reset(10 + index, f"absent-{index}@example.com").status_code == 401
    assert deliver_mail() == ANONYMOUS_MAIL_PER_HOUR


Answer = tuple[int, Any, dict[str, str | None]]


def resend_answers(*clients: Client) -> tuple[Answer, ...]:
    answers = []
    for client in clients:
        response = submit(client, RESEND, {})
        headers = {name: response.get(name) for name in ("Content-Type", "Retry-After")}
        answers.append((response.status_code, response.json(), headers))
    return tuple(answers)


@pytest.mark.parametrize("outcome", ["sent", "failed", "uncertain", "pending", "budget-skipped"])
@pytest.mark.parametrize(
    "start", [pending_signup, pending_address_change], ids=["signup", "address-change"]
)
def test_asking_again_for_a_code_answers_alike_for_an_address_with_and_without_an_account(
    transport: type[Transport],
    clock: Clock,
    start: Callable[[int, str], Client],
    outcome: str,
) -> None:
    if outcome == "budget-skipped":
        spend_anonymous_budget()
    taken = registered("taken@example.com")
    clients = start(0, "fresh@example.com"), start(1, taken.email)
    end_mails(transport, outcome)

    answers = []
    for _ in ("first resend", "second resend"):
        for seconds in (0, 59, 2):
            clock.advance(seconds)
            answers.append(resend_answers(*clients))
        end_mails(transport, outcome)

    assert [fresh for fresh, _ in answers] == [known for _, known in answers]
    assert [fresh[0] for fresh, _ in answers] == [429, 429, 200] * 2


@pytest.mark.parametrize("first_mail", ["undelivered", "sent"])
@pytest.mark.parametrize("outcome", ["failed", "uncertain"])
def test_a_second_resend_after_undelivered_codes_answers_alike_with_and_without_an_account(
    transport: type[Transport], clock: Clock, outcome: str, first_mail: str
) -> None:
    taken = registered("taken@example.com")
    clients = pending_signup(0, "fresh@example.com"), pending_signup(1, taken.email)
    end_mails(transport, outcome if first_mail == "undelivered" else "sent")
    if first_mail == "sent":
        clock.advance(61)

    first = resend_answers(*clients)
    end_mails(transport, outcome)
    second = resend_answers(*clients)

    assert first[0] == first[1]
    assert second[0] == second[1]
    assert (first[0][0], second[0][0]) == (200 if first_mail == "sent" else 429, 429)


@pytest.mark.parametrize("outcome", ["failed", "uncertain"])
def test_a_first_resend_after_a_notice_skipped_by_the_anonymous_budget_answers_alike(
    transport: type[Transport], clock: Clock, outcome: str
) -> None:
    from pyracast.accounts.models import OutboundMail

    spend_anonymous_budget()
    taken = registered("taken@example.com")
    clients = pending_signup(0, "fresh@example.com"), pending_signup(1, taken.email)
    end_mails(transport, outcome)
    ends = OutboundMail.objects.filter(recipient__in=("fresh@example.com", taken.email))
    assert list(ends.order_by("pk").values_list("kind", "status")) == [
        ("account/email/email_confirmation_signup", outcome),
        ("account/email/account_already_exists", "skipped"),
    ]
    assert ends.get(recipient=taken.email).last_error == "budget_exhausted"

    answers = resend_answers(*clients)

    assert answers[0] == answers[1]
    assert answers[0][0] == 429


def store_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records the key of every write to the store of allauth's rate limits ("*" for a clear)."""
    writes: list[str] = []

    def recorded(name: str) -> Callable[..., Any]:
        original = getattr(AuthenticationCache, name)

        def write(store: AuthenticationCache, *args: Any, **kwargs: Any) -> Any:
            writes.append(str(kwargs.get("key", args[0] if args else "*")))
            return original(store, *args, **kwargs)

        return write

    for name in ("add", "set", "delete", "clear"):
        monkeypatch.setattr(AuthenticationCache, name, recorded(name))
    return writes


def test_the_worker_writes_no_resend_window_even_when_a_late_refusal_ends_an_uncertain_code(
    transport: type[Transport], clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyracast.accounts.models import OutboundMail
    from pyracast.mail.errors import NotSentError
    from pyracast.notifications import outbox

    writes = store_writes(monkeypatch)
    client = Client(enforce_csrf_checks=True)
    assert signup(client).status_code == 401
    assert any(":confirm_email:" in key for key in writes)
    mail_id = OutboundMail.objects.get().pk
    writes.clear()

    def recovered_then_refused(message: EmailMessage, *, before_send: Any) -> None:
        assert before_send()
        # The lease ends while the relay answers and another worker recovers the intent.
        assert not outbox.send_outbound_mail(mail_id, after_the_lease())
        assert OutboundMail.objects.get().status == "uncertain"
        raise NotSentError("relay refused the message")

    with monkeypatch.context() as relay:
        relay.setattr(outbox, "send", recovered_then_refused)
        assert not outbox.send_outbound_mail(mail_id, timezone.now())

    assert OutboundMail.objects.get().status == "failed"
    assert [key for key in writes if key == "*" or ":confirm_email:" in key] == []
    clock.advance(59)
    assert submit(client, RESEND, {}).status_code == 429
    clock.advance(2)
    assert submit(client, RESEND, {}).status_code == 200


def test_a_reset_for_the_longest_accepted_address_answers_alike_and_is_mailed(
    transport: type[Transport],
) -> None:
    longest = request_reset(0, LONGEST_ADDRESS)
    short = request_reset(1, "absent@example.com")

    assert (longest.status_code, longest.json()) == (short.status_code, short.json())
    assert intents() == [
        ("account/email/unknown_account", LONGEST_ADDRESS),
        ("account/email/unknown_account", "absent@example.com"),
    ]
    assert deliver_mail() == 2
    assert transport.sent[0].to == [LONGEST_ADDRESS]


def test_an_address_change_to_the_longest_account_address_is_mailed_and_verified(
    transport: type[Transport],
) -> None:
    client = pending_address_change(0, LONGEST_ACCOUNT_ADDRESS)

    assert intents() == [("account/email/email_confirmation", LONGEST_ACCOUNT_ADDRESS)]
    assert deliver_mail() == 1
    assert transport.sent[0].to == [LONGEST_ACCOUNT_ADDRESS]
    code = mailed_code(transport.sent[0])
    assert submit(client, "auth/email/verify", {"key": code}).status_code == 200
    assert EmailAddress.objects.filter(email=LONGEST_ACCOUNT_ADDRESS, verified=True).exists()


@pytest.mark.parametrize("address", [OVERLONG_ACCOUNT_ADDRESS, LONGEST_ADDRESS], ids=["255", "320"])
def test_an_address_change_longer_than_an_account_address_is_refused_before_any_mail(
    address: str,
) -> None:
    client = reauthenticated_changer(0)

    response = submit(client, "account/email", {"email": address})

    assert response.status_code == 400
    assert [(error["code"], error["param"]) for error in response.json()["errors"]] == [
        ("max_length", "email")
    ]
    assert intents() == []
    assert not EmailAddress.objects.filter(email=address).exists()


def test_an_address_change_longer_than_any_address_names_only_the_account_limit() -> None:
    client = reauthenticated_changer(0)
    address = "a" + LONGEST_ADDRESS

    response = submit(client, "account/email", {"email": address})

    assert response.status_code == 400
    assert [
        error["message"] for error in response.json()["errors"] if error["code"] == "max_length"
    ] == [f"Ensure this value has at most 254 characters (it has {len(address)})."]
    assert intents() == []


@pytest.mark.parametrize("address", [OVERLONG_ACCOUNT_ADDRESS, "added@example.com"])
def test_an_address_change_is_checked_against_the_sessions_authority_before_its_input(
    address: str,
) -> None:
    client = reauthenticated_changer(0)
    user = User.objects.get(username="changer-0")
    EmailAddress.objects.create(user=user, email="spare@example.com", verified=True)

    response = submit(client, "account/email", {"email": address})

    assert response.status_code == 401
    assert [error["code"] for error in response.json()["errors"]] == ["reauthentication_required"]
    assert intents() == []
    assert deliver_mail() == 0


def test_a_late_refusal_never_reopens_an_intent_recovered_as_uncertain(
    transport: type[Transport], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyracast.accounts.models import OutboundMail
    from pyracast.mail.errors import NotSentError
    from pyracast.notifications import outbox

    request_reset(0, member().email)
    mail_id = OutboundMail.objects.get().pk

    def recovered_then_refused(message: EmailMessage, *, before_send: Any) -> None:
        assert before_send()
        # The lease ends while the relay answers and another worker recovers the intent.
        assert not outbox.send_outbound_mail(mail_id, after_the_lease())
        raise NotSentError("relay refused the message")

    monkeypatch.setattr(outbox, "send", recovered_then_refused)
    assert not outbox.send_outbound_mail(mail_id, timezone.now())
    monkeypatch.undo()

    assert deliver_mail(timezone.now() + timedelta(minutes=3)) == 0
    assert transport.sent == []
    assert list(OutboundMail.objects.values_list("status", "sealed_code")) == [("failed", "")]


def test_an_unsent_code_is_erased_when_it_expires(transport: type[Transport]) -> None:
    from pyracast.accounts.models import OutboundMail
    from pyracast.notifications.outbox import end_expired_mail

    request_reset(0, member().email)
    moment = timezone.now()

    assert end_expired_mail(moment) == 0
    assert end_expired_mail(moment + timedelta(seconds=601)) == 1
    assert deliver_mail(moment + timedelta(seconds=601)) == 0

    row = OutboundMail.objects.get()
    assert (row.status, row.last_error, row.sealed_code) == ("skipped", "expired", "")
    assert transport.sent == []


def test_the_database_keeps_codes_out_of_ended_intents_and_their_context() -> None:
    from pyracast.accounts.models import OutboundMail

    request_reset(0, member().email)
    request_reset(1, "absent@example.com")
    coded, notice = OutboundMail.objects.order_by("pk")

    for change in (
        {"status": "sent"},
        {"status": "failed"},
        {"context": {"code": "ABCD-EFGH"}},
        {"context": {"timestamp": 1}},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            OutboundMail.objects.filter(pk=coded.pk).update(**change)
    with pytest.raises(IntegrityError), transaction.atomic():
        OutboundMail.objects.filter(pk=notice.pk).update(sealed_code=coded.sealed_code)


def test_anonymous_account_mails_share_twenty_per_hour_in_the_worker(
    transport: type[Transport], pyracast_logs: pytest.LogCaptureFixture
) -> None:
    for index in range(11):
        existing = User.objects.create_user(
            username=f"existing-{index}", email=f"existing-{index}@example.com", password=PASSWORD
        )
        EmailAddress.objects.create(
            user=existing, email=existing.email, primary=True, verified=True
        )
    answers = {
        request_reset(index, f"absent-{index}@example.com").status_code for index in range(11)
    }
    answers |= {
        signup(
            visitor(20 + index),
            {"company_name": f"Company {index}", "email": f"existing-{index}@example.com"},
        ).status_code
        for index in range(11)
    }

    assert answers == {401}
    assert transport.sent == []
    assert deliver_mail() == 20
    skipped = [
        record.getMessage()
        for record in pyracast_logs.records
        if record.getMessage().startswith("outbound_mail_skipped")
    ]
    assert len(skipped) == 2
    assert all("reason=budget" in line and "@" not in line for line in skipped)
    assert deliver_mail(timezone.now() + timedelta(hours=2)) == 0


def test_concurrent_workers_keep_the_anonymous_budget(transport: type[Transport]) -> None:
    from pyracast.notifications.outbox import pending_mail_ids, send_outbound_mail

    for index in range(24):
        request_reset(index, f"absent-{index}@example.com")
    ids = pending_mail_ids(timezone.now())
    start = Barrier(4)

    def work(share: list[int]) -> int:
        close_old_connections()
        try:
            start.wait(timeout=10)
            return sum(send_outbound_mail(mail_id, timezone.now()) for mail_id in share)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(4) as pool:
        sent = sum(pool.map(work, [ids[part::4] for part in range(4)]))

    assert (sent, len(transport.sent)) == (20, 20)


def test_a_code_claimed_after_its_expiry_is_skipped_and_erased(transport: type[Transport]) -> None:
    from pyracast.accounts.models import OutboundMail
    from pyracast.notifications.outbox import send_outbound_mail

    request_reset(0, member().email)
    row = OutboundMail.objects.get()

    assert not send_outbound_mail(row.pk, timezone.now() + timedelta(seconds=601))

    row.refresh_from_db()
    assert (row.status, row.last_error, row.sealed_code) == ("skipped", "expired", "")
    assert transport.sent == []
