import logging
import re
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import Any, ClassVar

import pytest
from django.contrib.auth.models import User
from django.core.mail import EmailMessage
from django.core.mail.backends.base import BaseEmailBackend
from django.db import close_old_connections, connection, connections
from django.test import Client, override_settings
from django.utils import timezone
from psycopg import sql

from pyracast.accounts.invitations import (
    accept_workspace_invitation,
    invitation_digest,
    invite_workspace_member,
    revoke_workspace_invitation,
)
from pyracast.accounts.models import Membership, Tenant, WorkspaceInvitation, WorkspaceRole
from pyracast.errors import DomainError
from pyracast.forecasts.monitoring import delivery_health
from pyracast.mail.errors import NotSentError, PermanentlyNotSentError
from tests.support.outbox import after_the_lease, deliver_mail

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


class WorkerCrash(BaseException):
    pass


class Transport(BaseEmailBackend):
    sent: ClassVar[list[EmailMessage]] = []
    open_failure: ClassVar[BaseException | None] = None
    send_failure: ClassVar[BaseException | None] = None
    delay: ClassVar[float] = 0.0

    def open(self) -> bool:
        if self.open_failure is not None:
            raise self.open_failure
        return True

    def send_messages(self, email_messages: Sequence[EmailMessage] | None) -> int:
        assert email_messages is not None
        if self.send_failure is not None:
            raise self.send_failure
        time.sleep(self.delay)
        self.sent.extend(email_messages)
        return len(email_messages)


@pytest.fixture(autouse=True)
def transport() -> Iterator[type[Transport]]:
    Transport.sent = []
    Transport.open_failure = Transport.send_failure = None
    Transport.delay = 0.0
    with override_settings(MAILERS={"default": {"BACKEND": "tests.test_outbound_mail.Transport"}}):
        yield Transport


@pytest.fixture
def workspace() -> tuple[Tenant, User]:
    tenant = Tenant.objects.create(name="Solar workspace")
    administrator = User.objects.create_user(username="workspace-admin", email="admin@example.com")
    Membership.objects.create(tenant=tenant, user=administrator, role=WorkspaceRole.ADMIN)
    return tenant, administrator


def invite(workspace: tuple[Tenant, User]) -> WorkspaceInvitation:
    tenant, administrator = workspace
    return invite_workspace_member(administrator, tenant.pk, "guest@example.com")


def mailed_token(message: EmailMessage) -> str:
    match = re.search(r"token=([A-Za-z0-9_-]+)", str(message.body))
    assert match is not None
    return match.group(1)


def tables_holding(text: str) -> list[str]:
    found = []
    with connection.cursor() as cursor:
        for table in connection.introspection.table_names():
            cursor.execute(
                sql.SQL(
                    "SELECT count(*) FROM {} AS stored"
                    " WHERE strpos(row_to_json(stored)::text, %s) > 0"
                ).format(sql.Identifier(table)),
                [text],
            )
            if cursor.fetchone() != (0,):
                found.append(table)
    return found


def outbound_state() -> list[tuple[str, int, str]]:
    from pyracast.notifications.models import OutboundMail

    return list(OutboundMail.objects.order_by("pk").values_list("status", "attempts", "last_error"))


def test_an_invitation_is_answered_before_its_mail_is_sent(
    workspace: tuple[Tenant, User], transport: type[Transport]
) -> None:
    tenant, administrator = workspace
    client = Client()
    client.force_login(administrator)

    response = client.post(
        f"/api/tenants/{tenant.pk}/invitations/",
        {"email": "guest@example.com"},
        content_type="application/json",
    )

    assert (response.status_code, response.json()["delivery_status"]) == (201, "pending")
    assert transport.sent == []
    assert deliver_mail() == 1
    (message,) = transport.sent
    assert message.to == ["guest@example.com"]
    invitation = WorkspaceInvitation.objects.get()
    assert invitation.token_hash == invitation_digest(mailed_token(message))
    assert invitation.delivery_status == "sent"


def test_no_row_or_log_line_holds_the_invitation_token_or_link(
    workspace: tuple[Tenant, User],
    transport: type[Transport],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging.getLogger("pyracast"), "propagate", True)
    caplog.set_level(logging.DEBUG)
    transport.send_failure = NotSentError("relay refused the message")
    invite(workspace)
    deliver_mail()
    transport.send_failure = None
    deliver_mail(timezone.now() + timedelta(hours=1))

    (message,) = transport.sent
    token = mailed_token(message)
    link = re.search(r"\S*token=\S+", str(message.body))
    assert link is not None
    for secret in (token, link.group(0)):
        assert tables_holding(secret) == []
        assert not [record for record in caplog.records if secret in record.getMessage()]


def test_an_invitation_closed_before_its_mail_is_sent_is_not_mailed(
    workspace: tuple[Tenant, User], transport: type[Transport]
) -> None:
    tenant, administrator = workspace
    invitation = invite(workspace)
    revoke_workspace_invitation(administrator, tenant.pk, invitation.pk)

    assert deliver_mail() == 0

    assert transport.sent == []
    assert outbound_state() == [("skipped", 1, "invitation_unavailable")]
    assert WorkspaceInvitation.objects.get().token_hash is None


def test_a_definite_non_send_is_retried_with_a_fresh_token(
    workspace: tuple[Tenant, User], transport: type[Transport]
) -> None:
    invitation = invite(workspace)
    transport.send_failure = NotSentError("relay refused the connection")
    assert deliver_mail() == 0
    assert outbound_state() == [("retry", 1, "mail_not_sent")]
    first_digest = WorkspaceInvitation.objects.get().token_hash

    transport.send_failure = None
    assert deliver_mail(timezone.now() + timedelta(minutes=5)) == 1

    (message,) = transport.sent
    invitation.refresh_from_db()
    assert invitation.token_hash == invitation_digest(mailed_token(message)) != first_digest
    assert outbound_state() == [("sent", 2, "")]


def test_retries_end_after_a_bound_and_the_failure_is_reported(
    workspace: tuple[Tenant, User],
    transport: type[Transport],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyracast.notifications.outbox import MAX_ATTEMPTS

    monkeypatch.setattr(logging.getLogger("pyracast"), "propagate", True)
    caplog.set_level(logging.INFO)
    invite(workspace)
    transport.open_failure = NotSentError("relay unavailable")
    moment = timezone.now()
    for _ in range(MAX_ATTEMPTS + 2):
        deliver_mail(moment)
        moment += timedelta(hours=1)

    assert outbound_state() == [("failed", MAX_ATTEMPTS, "mail_not_sent")]
    assert WorkspaceInvitation.objects.get().delivery_status == "failed"
    (report,) = [
        record
        for record in caplog.records
        if record.getMessage().startswith("outbound_mail_failed")
    ]
    assert report.levelno == logging.ERROR
    assert "guest@example.com" not in report.getMessage()
    assert delivery_health(moment, timedelta(minutes=5))["failed_outbound_mail"] == 1


def test_a_permanent_refusal_fails_at_once(
    workspace: tuple[Tenant, User], transport: type[Transport]
) -> None:
    invite(workspace)
    transport.send_failure = PermanentlyNotSentError("mailbox unknown")

    deliver_mail()

    assert outbound_state() == [("failed", 1, "mail_rejected")]


def test_a_crash_before_transmission_is_retried_once_the_lease_ends(
    workspace: tuple[Tenant, User], transport: type[Transport]
) -> None:
    invite(workspace)
    transport.open_failure = WorkerCrash()
    with pytest.raises(WorkerCrash):
        deliver_mail()
    assert outbound_state() == [("sending", 1, "")]
    transport.open_failure = None

    assert deliver_mail() == 0
    assert deliver_mail(after_the_lease()) == 1

    assert len(transport.sent) == 1
    assert outbound_state() == [("sent", 2, "")]


def test_a_crash_during_transmission_is_never_sent_again(
    workspace: tuple[Tenant, User], transport: type[Transport]
) -> None:
    invite(workspace)
    transport.send_failure = WorkerCrash()
    with pytest.raises(WorkerCrash):
        deliver_mail()
    transport.send_failure = None

    for moment in (after_the_lease(), after_the_lease() + timedelta(hours=1)):
        assert deliver_mail(moment) == 0

    assert transport.sent == []
    assert outbound_state() == [("uncertain", 1, "interrupted_send")]
    assert WorkspaceInvitation.objects.get().delivery_status == "uncertain"
    health = delivery_health(timezone.now() + timedelta(hours=2), timedelta(minutes=5))
    assert health["failed_outbound_mail"] == 1


def test_concurrent_workers_send_one_intent_once(
    workspace: tuple[Tenant, User], transport: type[Transport]
) -> None:
    from pyracast.notifications.outbox import send_outbound_mail

    invitation = invite(workspace)
    (mail_id,) = invitation.outboundmail_set.values_list("pk", flat=True)
    transport.delay = 0.3
    start = Barrier(4)

    def work() -> bool:
        close_old_connections()
        try:
            start.wait(timeout=10)
            return send_outbound_mail(mail_id, timezone.now())
        finally:
            connections.close_all()

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: work(), range(4)))

    assert sorted(results) == [False, False, False, True]
    assert len(transport.sent) == 1
    assert outbound_state() == [("sent", 1, "")]


def test_an_accepted_invitation_keeps_the_token_of_its_only_send(
    workspace: tuple[Tenant, User], transport: type[Transport]
) -> None:
    tenant, _ = workspace
    invite(workspace)
    deliver_mail()
    guest = User.objects.create_user(username="guest", email="guest@example.com")
    guest.emailaddress_set.create(email=guest.email, verified=True, primary=True)

    membership = accept_workspace_invitation(guest, mailed_token(transport.sent[0]))

    assert membership.tenant_id == tenant.pk
    with pytest.raises(DomainError):
        accept_workspace_invitation(guest, "not-the-token")


def test_the_dispatcher_publishes_a_due_intent_once_a_minute(
    workspace: tuple[Tenant, User],
) -> None:
    from pyracast.notifications.outbox import publish_due_mail

    invite(workspace)
    published: list[int] = []
    moment = timezone.now()

    counts: list[Any] = [
        publish_due_mail(published.append, moment + timedelta(seconds=offset))
        for offset in (0, 2, 30, 61)
    ]

    assert counts == [1, 0, 0, 1]
    assert len(set(published)) == 1
