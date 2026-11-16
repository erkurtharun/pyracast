import smtplib
from collections.abc import Buffer, Iterator, Sequence
from email import policy
from email.parser import BytesParser

import pytest
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.test import override_settings

from pyracast.mail_delivery import NotSentError, PermanentlyNotSentError
from pyracast.notifications.mail import send_message
from pyracast.smtp import EmailBackend

BACKEND = "pyracast.smtp.EmailBackend"
RECIPIENTS = ["first@example.com", "second@example.com"]


class SMTP(smtplib.SMTP):
    def __init__(self) -> None:
        super().__init__()
        self.does_esmtp = True
        self.esmtp_features = {"size": "1000000"}
        self.recipient_responses: dict[str, tuple[int, bytes]] = {}
        self.sender_response = (250, b"ok")
        self.data_response = (250, b"accepted")
        self.data_error: Exception | None = None
        self.reset_error: Exception | None = None
        self.reset_response = (250, b"reset")
        self.envelope: list[str] = []
        self.content: list[bytes] = []
        self.options: Sequence[str] = []
        self.resets = 0
        self.closed = False

    def ehlo_or_helo_if_needed(self) -> None:
        pass

    def mail(self, sender: str, options: Sequence[str] = ()) -> tuple[int, bytes]:
        self.options = options
        return self.sender_response

    def rcpt(self, recip: str, options: Sequence[str] = ()) -> tuple[int, bytes]:
        self.envelope.append(recip)
        return self.recipient_responses.get(recip, (250, b"ok"))

    def data(self, msg: Buffer | str) -> tuple[int, bytes]:
        assert isinstance(msg, bytes)
        self.content.append(msg)
        if self.data_error is not None:
            raise self.data_error
        return self.data_response

    def rset(self) -> tuple[int, bytes]:
        self.resets += 1
        if self.reset_error is not None:
            raise self.reset_error
        return self.reset_response

    def quit(self) -> tuple[int, bytes]:
        self.close()
        return 221, b"bye"

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> SMTP:
    connection = SMTP()
    monkeypatch.setattr(smtplib, "SMTP", lambda *args, **kwargs: connection)
    return connection


def message() -> EmailMultiAlternatives:
    return EmailMultiAlternatives(
        subject="Saved forecast",
        body="Report",
        from_email="Pyracast <sender@example.com>",
        bcc=RECIPIENTS,
    )


@pytest.mark.parametrize(("code", "failure"), [(450, NotSentError), (550, PermanentlyNotSentError)])
def test_partial_recipient_rejection_aborts_before_data(
    smtp: SMTP, code: int, failure: type[Exception]
) -> None:
    smtp.recipient_responses[RECIPIENTS[1]] = code, b"rejected"
    with pytest.raises(failure):
        send_message(message())
    assert smtp.envelope == RECIPIENTS
    assert smtp.content == []
    assert smtp.resets == 1


@pytest.fixture(autouse=True)
def mailer_settings() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": BACKEND, "OPTIONS": {"host": "localhost"}}}
    ):
        yield


def test_group_acceptance_sends_one_message_with_private_recipients(smtp: SMTP) -> None:
    send_message(message())
    assert smtp.envelope == RECIPIENTS
    assert len(smtp.content) == 1
    parsed = BytesParser(policy=policy.default).parsebytes(smtp.content[0])
    assert parsed["From"] == "Pyracast <sender@example.com>"
    assert parsed["To"] is None
    assert parsed["Cc"] is None
    assert parsed["Bcc"] is None
    assert parsed.get_content() == "Report\r\n"
    assert smtp.options == [f"size={len(smtp.content[0])}"]
    assert smtp.resets == 0


def test_forwarded_recipient_and_plain_smtp_remain_compatible(smtp: SMTP) -> None:
    smtp.does_esmtp = False
    smtp.esmtp_features = {}
    smtp.recipient_responses[RECIPIENTS[1]] = 251, b"forwarded"
    outgoing = message()
    outgoing.to, outgoing.bcc = [RECIPIENTS[0]], [RECIPIENTS[1]]
    send_message(outgoing)
    assert len(smtp.content) == 1
    assert smtp.envelope == RECIPIENTS
    assert smtp.options == []


def test_single_transactional_recipient_keeps_its_visible_header(smtp: SMTP) -> None:
    outgoing = message()
    outgoing.to, outgoing.bcc = [RECIPIENTS[0]], []
    send_message(outgoing)
    assert smtp.envelope == RECIPIENTS[:1]
    assert len(smtp.content) == 1
    parsed = BytesParser(policy=policy.default).parsebytes(smtp.content[0])
    assert parsed["To"] == RECIPIENTS[0]
    assert parsed["Bcc"] is None


@pytest.mark.parametrize(
    ("code", "failure"),
    [(450, NotSentError), (550, PermanentlyNotSentError), (-1, smtplib.SMTPException)],
)
@pytest.mark.parametrize("phase", ["sender", "data"])
def test_explicit_rejections_preserve_retry_classification(
    smtp: SMTP, code: int, failure: type[Exception], phase: str
) -> None:
    setattr(smtp, phase + "_response", (code, b"rejected"))
    with pytest.raises(failure):
        send_message(message())
    assert len(smtp.content) == (1 if phase == "data" else 0)
    assert smtp.resets == 1


@pytest.mark.parametrize("phase", ["sender", "recipient", "data"])
def test_server_closing_rejection_aborts_without_reset(smtp: SMTP, phase: str) -> None:
    if phase == "recipient":
        smtp.recipient_responses[RECIPIENTS[0]] = 421, b"closing"
    else:
        setattr(smtp, phase + "_response", (421, b"closing"))
    with pytest.raises(NotSentError):
        send_message(message())
    assert len(smtp.content) == (1 if phase == "data" else 0)
    assert smtp.resets == 0
    assert smtp.closed
    if phase == "recipient":
        assert smtp.envelope == RECIPIENTS[:1]


@pytest.mark.parametrize("reset_failure", ["disconnect", "rejection"])
def test_reset_failure_preserves_known_nonacceptance(smtp: SMTP, reset_failure: str) -> None:
    smtp.recipient_responses[RECIPIENTS[1]] = 450, b"retry"
    if reset_failure == "disconnect":
        smtp.reset_error = smtplib.SMTPServerDisconnected()
    else:
        smtp.reset_response = 500, b"failed"
    with pytest.raises(NotSentError):
        send_message(message())
    assert smtp.content == []
    assert smtp.closed


@pytest.mark.parametrize("failure", [TimeoutError(), smtplib.SMTPServerDisconnected()])
def test_lost_data_acknowledgement_is_not_classified_as_safe_to_retry(
    smtp: SMTP, failure: Exception
) -> None:
    smtp.data_error = failure
    with pytest.raises(type(failure)):
        send_message(message())
    assert len(smtp.content) == 1
    assert smtp.resets == 0


def test_silent_rejection_returns_no_sent_message(smtp: SMTP) -> None:
    backend = mail.mailers["default"]
    assert isinstance(backend, EmailBackend)
    backend.fail_silently = True
    smtp.recipient_responses[RECIPIENTS[1]] = 550, b"rejected"
    assert backend.send_messages([message()]) == 0
    assert smtp.content == []


def test_empty_recipient_list_does_not_start_a_transaction(smtp: SMTP) -> None:
    outgoing = message()
    outgoing.bcc = []
    assert mail.mailers["default"].send_messages([outgoing]) == 0
    assert smtp.envelope == []
    assert smtp.content == []
