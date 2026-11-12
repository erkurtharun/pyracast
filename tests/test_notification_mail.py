import smtplib

import pytest
from django.core.mail import EmailMultiAlternatives
from django.test import override_settings

from pyracast.notifications.mail import (
    NotSentError,
    PermanentlyNotSentError,
    SendCancelledError,
    send_message,
)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[SMTP]:
    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    SMTP.open_error = SMTP.send_error = SMTP.close_error = None
    SMTP.accepted = 0
    SMTP.closed = False
    return SMTP


class SMTP:
    open_error: Exception | None = None
    send_error: Exception | None = None
    close_error: Exception | None = None
    accepted = 0
    closed = False

    def __init__(self, *args: object, **kwargs: object) -> None:
        if self.open_error is not None:
            raise self.open_error

    def sendmail(self, *args: object, **kwargs: object) -> dict[str, tuple[int, bytes]]:
        if self.send_error is not None:
            raise self.send_error
        type(self).accepted += 1
        return {}

    def quit(self) -> None:
        type(self).closed = True
        if self.close_error is not None:
            raise self.close_error


@pytest.mark.parametrize(
    ("phase", "error", "failure"),
    [
        ("open", ConnectionRefusedError(), NotSentError),
        ("open", TimeoutError(), NotSentError),
        ("open", smtplib.SMTPAuthenticationError(454, b"temporary"), NotSentError),
        ("open", smtplib.SMTPAuthenticationError(535, b"secret"), NotSentError),
        ("open", smtplib.SMTPNotSupportedError(), NotSentError),
        ("send", smtplib.SMTPRecipientsRefused({"to@example.com": (450, b"wait")}), NotSentError),
        (
            "send",
            smtplib.SMTPRecipientsRefused({"to@example.com": (550, b"secret")}),
            PermanentlyNotSentError,
        ),
        ("send", smtplib.SMTPDataError(451, b"wait"), NotSentError),
        ("send", smtplib.SMTPDataError(554, b"secret"), PermanentlyNotSentError),
        (
            "send",
            smtplib.SMTPSenderRefused(550, b"secret", "from@example.com"),
            PermanentlyNotSentError,
        ),
        ("send", smtplib.SMTPHeloError(450, b"wait"), NotSentError),
        ("send", smtplib.SMTPNotSupportedError(), PermanentlyNotSentError),
        ("send", smtplib.SMTPServerDisconnected(), smtplib.SMTPServerDisconnected),
        ("send", TimeoutError(), TimeoutError),
    ],
)
def test_smtp_classifies_acceptance_at_the_real_django_backend_boundary(
    smtp: type[SMTP], phase: str, error: Exception, failure: type[Exception]
) -> None:
    setattr(smtp, phase + "_error", error)
    with (
        override_settings(
            MAILERS={
                "default": {
                    "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
                    "OPTIONS": {"host": "localhost"},
                }
            }
        ),
        pytest.raises(failure) as caught,
    ):
        send_message(
            EmailMultiAlternatives(subject="Forecast", body="Saved", to=["to@example.com"])
        )
    assert smtp.accepted == 0
    if isinstance(caught.value, NotSentError):
        assert "secret" not in str(caught.value)
    assert smtp.closed is (phase == "send")


def test_smtp_quit_failure_does_not_discard_confirmed_acceptance(smtp: type[SMTP]) -> None:
    smtp.close_error = smtplib.SMTPResponseException(421, b"closing")
    with override_settings(
        MAILERS={
            "default": {
                "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
                "OPTIONS": {"host": "localhost"},
            }
        }
    ):
        send_message(
            EmailMultiAlternatives(subject="Forecast", body="Saved", to=["to@example.com"])
        )
    assert smtp.accepted == 1
    assert smtp.closed


@pytest.mark.parametrize("failed_check", [False, True])
def test_smtp_does_not_send_when_the_open_connection_loses_transmission_authority(
    smtp: type[SMTP],
    failed_check: bool,
) -> None:
    def check() -> bool:
        if failed_check:
            raise RuntimeError("A database check could not finish.")
        return False

    with (
        override_settings(
            MAILERS={
                "default": {
                    "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
                    "OPTIONS": {"host": "localhost"},
                }
            }
        ),
        pytest.raises(NotSentError if failed_check else SendCancelledError) as caught,
    ):
        send_message(
            EmailMultiAlternatives(subject="Forecast", body="Saved", to=["to@example.com"]),
            before_send=check,
        )
    assert smtp.accepted == 0
    assert smtp.closed
    if isinstance(caught.value, NotSentError):
        assert caught.value.code == "mail_preparation_failed"
