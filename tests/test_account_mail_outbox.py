import re
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import Any, ClassVar

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.mail import EmailMessage
from django.core.mail.backends.base import BaseEmailBackend
from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from django.test import Client, override_settings
from django.utils import timezone
from psycopg import sql

from pyracast.mail.errors import PermanentlyNotSentError
from tests.support.auth_lifecycle import LONGEST_ADDRESS, PASSWORD, member, submit
from tests.support.company_registration import signup
from tests.support.outbox import after_the_lease, deliver_mail

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


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


def visitor(index: int) -> Client:
    return Client(enforce_csrf_checks=True, REMOTE_ADDR=f"198.51.100.{index + 1}")


def request_reset(index: int, email: str) -> Any:
    return submit(visitor(index), "auth/password/request", {"email": email})


def intents() -> list[tuple[str, str]]:
    from pyracast.notifications.models import OutboundMail

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


def test_a_mailed_code_rests_only_sealed_and_is_erased_once_sent(
    transport: type[Transport],
) -> None:
    from pyracast.notifications.models import OutboundMail
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
    from pyracast.notifications.models import OutboundMail

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


def test_a_verification_code_whose_mail_ends_uncertain_at_once_can_be_asked_again_at_once(
    transport: type[Transport],
) -> None:
    client = Client(enforce_csrf_checks=True)
    assert signup(client).status_code == 401
    transport.failure_after_acceptance = TimeoutError("relay answer lost")
    assert deliver_mail() == 0
    transport.failure_after_acceptance = None
    lost = mailed_code(transport.sent[0])

    assert submit(client, "auth/email/verify/resend", {}).status_code == 200
    assert deliver_mail() == 1
    code = mailed_code(transport.sent[-1])
    assert code != lost
    assert submit(client, "auth/email/verify", {"key": code}).status_code == 200


def pending_signup(index: int, address: str) -> Client:
    client = visitor(index)
    details = {"company_name": f"Company {index}", "email": address}
    assert signup(client, details).status_code == 401
    return client


def pending_address_change(index: int, address: str) -> Client:
    user = User.objects.create_user(
        username=f"changer-{index}", email=f"changer-{index}@example.com", password=PASSWORD
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    client = visitor(index)
    credentials = {"email": user.email, "password": PASSWORD}
    assert submit(client, "auth/login", credentials).status_code == 200
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    assert submit(client, "account/email", {"email": address}).status_code == 200
    return client


def end_first_mails(transport: type[Transport], outcome: str) -> None:
    if outcome == "pending":
        return
    if outcome == "failed":
        transport.refusal = PermanentlyNotSentError("relay refused the message")
    elif outcome == "uncertain":
        transport.failure_after_acceptance = TimeoutError("relay answer lost")
    assert deliver_mail() == (2 if outcome == "sent" else 0)
    transport.refusal = transport.failure_after_acceptance = None


def resend_answer(client: Client) -> tuple[int, Any, dict[str, str | None]]:
    response = submit(client, "auth/email/verify/resend", {})
    headers = {name: response.get(name) for name in ("Content-Type", "Retry-After")}
    return response.status_code, response.json(), headers


@pytest.mark.parametrize("outcome", ["sent", "failed", "uncertain", "pending"])
@pytest.mark.parametrize(
    "start", [pending_signup, pending_address_change], ids=["signup", "address-change"]
)
def test_asking_again_for_a_code_answers_alike_for_an_address_with_and_without_an_account(
    transport: type[Transport], start: Callable[[int, str], Client], outcome: str
) -> None:
    taken = User.objects.create_user(username="taken", email="taken@example.com", password=PASSWORD)
    EmailAddress.objects.create(user=taken, email=taken.email, primary=True, verified=True)
    unknown = start(0, "fresh@example.com")
    known = start(1, taken.email)
    end_first_mails(transport, outcome)

    answers = resend_answer(unknown), resend_answer(known)

    assert answers[0] == answers[1]
    assert answers[0][0] == (200 if outcome in ("failed", "uncertain") else 429)


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


def test_an_address_change_to_the_longest_accepted_address_mails_its_code(
    transport: type[Transport],
) -> None:
    pending_address_change(0, LONGEST_ADDRESS)

    assert intents() == [("account/email/email_confirmation", LONGEST_ADDRESS)]
    assert deliver_mail() == 1
    assert transport.sent[0].to == [LONGEST_ADDRESS]


def test_a_late_refusal_never_reopens_an_intent_recovered_as_uncertain(
    transport: type[Transport], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyracast.mail.errors import NotSentError
    from pyracast.notifications import outbox
    from pyracast.notifications.models import OutboundMail

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
    from pyracast.notifications.models import OutboundMail
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
    from pyracast.notifications.models import OutboundMail

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
    from pyracast.notifications.models import OutboundMail
    from pyracast.notifications.outbox import send_outbound_mail

    request_reset(0, member().email)
    row = OutboundMail.objects.get()

    assert not send_outbound_mail(row.pk, timezone.now() + timedelta(seconds=601))

    row.refresh_from_db()
    assert (row.status, row.last_error, row.sealed_code) == ("skipped", "expired", "")
    assert transport.sent == []
