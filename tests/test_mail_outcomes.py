from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

import pytest
from django.contrib.auth.models import User
from django.core.mail import EmailMessage
from django.core.mail.backends.base import BaseEmailBackend
from django.template import TemplateSyntaxError
from django.test import Client, override_settings

from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import Membership, Tenant, WorkspaceInvitation
from pyracast.mail.errors import (
    DeliveryUncertainError,
    MailFailure,
    NotSentError,
    PermanentlyNotSentError,
)
from pyracast.mail.send import send
from pyracast.notifications.jobs import process_notification
from tests.smtp_server import Script, loopback_smtp
from tests.support.auth_lifecycle import submit
from tests.support.notifications import SLOT, ready

SMTP_BACKEND = "pyracast.mail.smtp.EmailBackend"


def relay_options(port: int) -> dict[str, Any]:
    return {
        "host": "127.0.0.1",
        "port": port,
        "use_tls": False,
        "timeout": 5,
        "username": "relay-user",
        "password": "relay-secret",
    }


@pytest.fixture
def relay() -> Iterator[Script]:
    script = Script(extensions=["8BITMIME", "SIZE 10000000", "AUTH PLAIN"])
    with (
        loopback_smtp(script) as port,
        override_settings(
            MAILERS={"default": {"BACKEND": SMTP_BACKEND, "OPTIONS": relay_options(port)}}
        ),
    ):
        yield script


def message() -> EmailMessage:
    return EmailMessage("Forecast", "Saved", "Pyracast <info@pyracast.app>", ["to@example.com"])


def script_reply(script: Script, phase: str, reply: str) -> None:
    if phase == "mail":
        script.mail_reply = reply
    elif phase == "rcpt":
        script.recipient_replies["to@example.com"] = reply
    else:
        script.data_reply = reply


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        ("535 5.7.8 credentials rejected", MailFailure.CREDENTIALS_REJECTED),
        ("454 4.7.0 try again later", MailFailure.NOT_SENT),
    ],
)
def test_authentication_reply_decides_the_failure_code(
    relay: Script, reply: str, code: MailFailure
) -> None:
    relay.auth_reply = reply

    with pytest.raises(NotSentError) as caught:
        send(message())

    assert type(caught.value) is NotSentError
    assert caught.value.code == code
    assert "relay-secret" not in str(caught.value)
    assert relay.transactions == []


@pytest.mark.parametrize("phase", ["connect", "ehlo", "auth", "mail", "rcpt"])
def test_disconnect_before_data_is_a_known_non_send(relay: Script, phase: str) -> None:
    relay.drop_at = phase

    with pytest.raises(NotSentError) as caught:
        send(message())

    assert type(caught.value) is NotSentError
    assert caught.value.code == MailFailure.NOT_SENT
    assert relay.transactions == []


@pytest.mark.parametrize("phase", ["data", "data_end"])
def test_disconnect_once_data_begins_is_uncertain(relay: Script, phase: str) -> None:
    relay.drop_at = phase

    with pytest.raises(DeliveryUncertainError) as caught:
        send(message())

    assert caught.value.code == MailFailure.OUTCOME_UNCERTAIN


@pytest.mark.parametrize(
    ("phase", "reply", "failure", "code"),
    [
        ("mail", "451 4.3.0 try later", NotSentError, MailFailure.NOT_SENT),
        ("mail", "550 5.7.1 sender refused", PermanentlyNotSentError, MailFailure.REJECTED),
        ("mail", "199 not a transaction reply", NotSentError, MailFailure.NOT_SENT),
        ("rcpt", "450 4.2.0 mailbox busy", NotSentError, MailFailure.NOT_SENT),
        ("rcpt", "550 5.1.1 unknown mailbox", PermanentlyNotSentError, MailFailure.REJECTED),
        ("data", "452 4.3.1 storage full", NotSentError, MailFailure.NOT_SENT),
        ("data", "554 5.6.0 content refused", PermanentlyNotSentError, MailFailure.REJECTED),
        (
            "data",
            "199 not a transaction reply",
            DeliveryUncertainError,
            MailFailure.OUTCOME_UNCERTAIN,
        ),
    ],
)
def test_transaction_reply_decides_the_outcome_per_phase(
    relay: Script, phase: str, reply: str, failure: type[Exception], code: MailFailure
) -> None:
    script_reply(relay, phase, reply)

    with pytest.raises(failure) as caught:
        send(message())

    assert type(caught.value) is failure
    assert getattr(caught.value, "code", None) == code
    assert "refused" not in str(caught.value)
    assert reply.partition(" ")[2] not in str(caught.value)


def test_accepted_message_reaches_the_relay_once(relay: Script) -> None:
    send(message())

    (transaction,) = relay.transactions
    assert transaction.recipients == ["to@example.com"]
    assert relay.commands.count("AUTH") == 1


def test_invalid_mailer_configuration_is_a_retryable_non_send() -> None:
    options = {"host": "127.0.0.1", "use_tls": True, "use_ssl": True}
    with (
        override_settings(MAILERS={"default": {"BACKEND": SMTP_BACKEND, "OPTIONS": options}}),
        pytest.raises(NotSentError) as caught,
    ):
        send(message())

    assert type(caught.value) is NotSentError
    assert caught.value.code == MailFailure.CONFIGURATION_INVALID


class ScriptedBackend(BaseEmailBackend):
    open_failure: ClassVar[Exception | None] = None
    send_failure: ClassVar[Exception | None] = None
    sent: ClassVar[list[EmailMessage]] = []

    def open(self) -> bool:
        if self.open_failure is not None:
            raise self.open_failure
        return True

    def send_messages(self, email_messages: Sequence[EmailMessage] | None) -> int:
        assert email_messages is not None
        if self.send_failure is not None:
            raise self.send_failure
        self.sent.extend(email_messages)
        return len(email_messages)


@pytest.fixture
def scripted() -> Iterator[type[ScriptedBackend]]:
    ScriptedBackend.open_failure = ScriptedBackend.send_failure = None
    ScriptedBackend.sent = []
    with override_settings(
        MAILERS={"default": {"BACKEND": "tests.test_mail_outcomes.ScriptedBackend"}}
    ):
        yield ScriptedBackend


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("phase", "reply", "status", "code"),
    [
        ("mail", "451 4.3.0 try later", "retry", "mail_not_sent"),
        ("rcpt", "550 5.1.1 unknown mailbox", "failed", "mail_rejected"),
        ("data", "554 5.6.0 content refused", "failed", "mail_rejected"),
        ("data", "199 not a transaction reply", "uncertain", "mail_outcome_uncertain"),
    ],
)
def test_reminder_stores_the_relay_outcome(
    relay: Script, phase: str, reply: str, status: str, code: str
) -> None:
    delivery = ready()
    if phase == "rcpt":
        relay.recipient_replies = dict.fromkeys(delivery.recipients, reply)
    else:
        script_reply(relay, phase, reply)

    process_notification(delivery.pk, SLOT)

    delivery.refresh_from_db()
    assert (delivery.status, delivery.last_error) == (status, code)


@pytest.mark.django_db(transaction=True)
def test_reminder_retries_an_invalid_mailer_configuration() -> None:
    delivery = ready()
    options = {"host": "127.0.0.1", "use_tls": True, "use_ssl": True}
    with override_settings(MAILERS={"default": {"BACKEND": SMTP_BACKEND, "OPTIONS": options}}):
        process_notification(delivery.pk, SLOT)

    delivery.refresh_from_db()
    assert (delivery.status, delivery.last_error) == ("retry", "mail_configuration_invalid")
    assert delivery.transmit_started_at is None


@pytest.mark.django_db(transaction=True)
def test_unclassified_failure_before_the_transmit_marker_is_a_retry(
    scripted: type[ScriptedBackend],
) -> None:
    delivery = ready()
    scripted.open_failure = RuntimeError("private relay detail")

    process_notification(delivery.pk, SLOT)

    delivery.refresh_from_db()
    assert (delivery.status, delivery.last_error) == ("retry", "mail_not_sent")
    assert delivery.transmit_started_at is None


@pytest.mark.django_db(transaction=True)
def test_unclassified_failure_after_the_transmit_marker_is_uncertain(
    scripted: type[ScriptedBackend],
) -> None:
    delivery = ready()
    scripted.send_failure = RuntimeError("private relay detail")

    process_notification(delivery.pk, SLOT)

    delivery.refresh_from_db()
    assert (delivery.status, delivery.last_error) == ("uncertain", "mail_outcome_uncertain")
    assert delivery.transmit_started_at is not None


@pytest.fixture
def workspace() -> tuple[Tenant, User]:
    tenant = Tenant.objects.create(name="Solar")
    administrator = User.objects.create_user(username="administrator", email="admin@example.com")
    Membership.objects.create(tenant=tenant, user=administrator, role=Membership.Role.ADMIN)
    return tenant, administrator


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (None, "accepted"),
        (NotSentError("private relay detail"), "failed"),
        (PermanentlyNotSentError("private relay detail"), "failed"),
        (DeliveryUncertainError("private relay detail"), "uncertain"),
        (RuntimeError("private relay detail"), "uncertain"),
    ],
)
def test_invitation_records_the_delivery_outcome_per_error_class(
    scripted: type[ScriptedBackend],
    workspace: tuple[Tenant, User],
    failure: Exception | None,
    status: str,
) -> None:
    tenant, administrator = workspace
    scripted.send_failure = failure

    invitation = invite_workspace_member(administrator, tenant.pk, "guest@example.com")

    assert invitation.delivery_status == status
    assert WorkspaceInvitation.objects.get().delivery_status == status
    assert [message.to for message in scripted.sent] == (
        [["guest@example.com"]] if failure is None else []
    )


@pytest.mark.django_db(transaction=True)
def test_invitation_template_error_leaves_no_invitation(
    scripted: type[ScriptedBackend],
    workspace: tuple[Tenant, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, administrator = workspace

    def broken(*_args: object, **_kwargs: object) -> str:
        raise TemplateSyntaxError("broken template")

    monkeypatch.setattr("pyracast.accounts.invitations.render_to_string", broken)

    with pytest.raises(TemplateSyntaxError):
        invite_workspace_member(administrator, tenant.pk, "guest@example.com")

    assert not WorkspaceInvitation.objects.exists()
    assert scripted.sent == []


class ClosingBackend(BaseEmailBackend):
    delivered: ClassVar[int] = 1
    close_failure: ClassVar[OSError | None] = None
    sent: ClassVar[list[EmailMessage]] = []
    connected = False

    def open(self) -> bool:
        if self.connected:
            return False
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False
        if self.close_failure is not None:
            raise self.close_failure

    def send_messages(self, email_messages: Sequence[EmailMessage] | None) -> int:
        assert email_messages is not None
        created = self.open()
        try:
            self.sent.extend(email_messages)
            return self.delivered
        finally:
            if created:
                self.close()


@pytest.fixture
def closing() -> Iterator[type[ClosingBackend]]:
    ClosingBackend.delivered, ClosingBackend.close_failure = 1, None
    ClosingBackend.sent = []
    with override_settings(
        MAILERS={"default": {"BACKEND": "tests.test_mail_outcomes.ClosingBackend"}}
    ):
        yield ClosingBackend


ACCOUNT_MAIL = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def request_unknown_account_notice() -> None:
    response = submit(
        Client(enforce_csrf_checks=True), "auth/password/request", {"email": "missing@example.com"}
    )
    assert response.status_code == 401


def account_mail_failures(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("account_mail_failed ")
    ]


@ACCOUNT_MAIL
def test_account_mail_reports_an_invalid_mailer_configuration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    options = {"host": "127.0.0.1", "use_tls": True, "use_ssl": True}
    with override_settings(MAILERS={"default": {"BACKEND": SMTP_BACKEND, "OPTIONS": options}}):
        request_unknown_account_notice()

    (failure,) = account_mail_failures(caplog)
    assert failure.endswith(" error_type=NotSentError error_code=mail_configuration_invalid")


@ACCOUNT_MAIL
def test_account_mail_reports_a_backend_that_sent_nothing(
    closing: type[ClosingBackend], caplog: pytest.LogCaptureFixture
) -> None:
    closing.delivered = 0

    request_unknown_account_notice()

    (failure,) = account_mail_failures(caplog)
    assert failure.endswith(" error_type=NotSentError error_code=mail_not_sent")


@ACCOUNT_MAIL
def test_account_mail_accepted_before_a_failed_close_is_delivered(
    closing: type[ClosingBackend], caplog: pytest.LogCaptureFixture
) -> None:
    closing.close_failure = ConnectionResetError("relay closed the connection")

    request_unknown_account_notice()

    assert [message.to for message in closing.sent] == [["missing@example.com"]]
    assert account_mail_failures(caplog) == []


@ACCOUNT_MAIL
def test_account_mail_logs_the_uncertain_delivery_code(
    scripted: type[ScriptedBackend], caplog: pytest.LogCaptureFixture
) -> None:
    scripted.send_failure = DeliveryUncertainError("private relay detail")

    request_unknown_account_notice()

    (failure,) = account_mail_failures(caplog)
    assert failure.endswith(" error_type=DeliveryUncertainError error_code=mail_outcome_uncertain")
    assert "private relay detail" not in caplog.text


@pytest.mark.django_db(transaction=True)
def test_uncertain_reminder_delivery_logs_the_delivery_id(
    scripted: type[ScriptedBackend], caplog: pytest.LogCaptureFixture
) -> None:
    delivery = ready()
    scripted.send_failure = DeliveryUncertainError("private relay detail")

    assert not process_notification(delivery.pk, SLOT)

    delivery.refresh_from_db()
    assert (delivery.status, delivery.last_error) == ("uncertain", "mail_outcome_uncertain")
    assert f"notification_delivery_uncertain delivery_id={delivery.pk}" in caplog.messages
    assert "private relay detail" not in caplog.text
