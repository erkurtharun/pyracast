import re
from collections.abc import Iterator, Sequence
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

from tests.support.auth_lifecycle import PASSWORD, member, submit
from tests.support.company_registration import signup
from tests.support.outbox import after_the_lease, deliver_mail

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


class WorkerCrash(BaseException):
    pass


class Transport(BaseEmailBackend):
    sent: ClassVar[list[EmailMessage]] = []
    crash_after_acceptance: ClassVar[bool] = False

    def send_messages(self, email_messages: Sequence[EmailMessage] | None) -> int:
        assert email_messages is not None
        self.sent.extend(email_messages)
        if self.crash_after_acceptance:
            raise WorkerCrash
        return len(email_messages)


@pytest.fixture(autouse=True)
def transport() -> Iterator[type[Transport]]:
    Transport.sent = []
    Transport.crash_after_acceptance = False
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
