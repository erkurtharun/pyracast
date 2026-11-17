import smtplib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from runpy import run_path
from threading import Barrier, get_ident

import pytest
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.test import override_settings

from pyracast import settings as project_settings
from pyracast.mail.errors import NotSentError, SendCancelledError
from pyracast.mail.send import send
from pyracast.mail.smtp import EmailBackend
from pyracast.notifications.mail import build_message
from pyracast.notifications.models import NotificationDelivery


@override_settings(DEFAULT_FROM_EMAIL="Pyracast <info@pyracast.app>")
def test_pending_csv_delivery_preserves_its_frozen_content_after_the_xlsx_upgrade() -> None:
    delivery = NotificationDelivery(
        id=17,
        recipients=["recipient@example.com"],
        prepared_message={
            "subject": "Saved forecast",
            "body": "Frozen text",
            "html": "<p>Frozen text</p>",
            "csv_filename": "irradiance-2026-11-15.csv",
            "csv": "start,ghi_wm2\n2026-11-15T10:00:00+03:00,250\n",
        },
    )
    message = build_message(delivery)
    assert message.subject == "Saved forecast"
    assert message.body == "Frozen text"
    assert message.attachments[0] == (
        "irradiance-2026-11-15.csv",
        "start,ghi_wm2\n2026-11-15T10:00:00+03:00,250\n",
        "text/csv",
    )
    assert message.extra_headers["Message-ID"] == (
        f"<reminder.{delivery.message_token}@pyracast.app>"
    )


def test_default_smtp_configuration_has_a_bounded_socket_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYRACAST_EMAIL_BACKEND", "smtp")
    configuration = run_path(str(Path(project_settings.__file__)))
    mailer = configuration["MAILERS"]["default"]
    assert mailer["BACKEND"] == "pyracast.mail.smtp.EmailBackend"
    with override_settings(MAILERS=configuration["MAILERS"]):
        backend = mail.mailers["default"]
    assert isinstance(backend, EmailBackend)
    assert backend.timeout == 15
    assert backend.connection is None


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

    def ehlo_or_helo_if_needed(self) -> None:
        if self.send_error is not None:
            raise self.send_error

    def has_extn(self, name: str) -> bool:
        return False

    def mail(self, sender: str, options: list[str]) -> tuple[int, bytes]:
        return 250, b"OK"

    def rcpt(self, recipient: str) -> tuple[int, bytes]:
        return 250, b"OK"

    def data(self, content: bytes) -> tuple[int, bytes]:
        type(self).accepted += 1
        return 250, b"Accepted"

    def quit(self) -> None:
        type(self).closed = True
        if self.close_error is not None:
            raise self.close_error


def test_smtp_quit_failure_does_not_discard_confirmed_acceptance(smtp: type[SMTP]) -> None:
    smtp.close_error = smtplib.SMTPResponseException(421, b"closing")
    with override_settings(
        MAILERS={
            "default": {
                "BACKEND": "pyracast.mail.smtp.EmailBackend",
                "OPTIONS": {"host": "localhost"},
            }
        }
    ):
        send(EmailMultiAlternatives(subject="Forecast", body="Saved", to=["to@example.com"]))
    assert smtp.accepted == 1
    assert smtp.closed


def test_zero_backend_deliveries_are_reported_as_known_non_acceptance(
    smtp: type[SMTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(EmailBackend, "send_messages", lambda self, messages: 0)
    with (
        override_settings(
            MAILERS={
                "default": {
                    "BACKEND": "pyracast.mail.smtp.EmailBackend",
                    "OPTIONS": {"host": "localhost"},
                }
            }
        ),
        pytest.raises(NotSentError, match="did not send"),
    ):
        send(EmailMultiAlternatives(subject="Forecast", body="Saved", to=["to@example.com"]))
    assert smtp.accepted == 0
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
                    "BACKEND": "pyracast.mail.smtp.EmailBackend",
                    "OPTIONS": {"host": "localhost"},
                }
            }
        ),
        pytest.raises(NotSentError if failed_check else SendCancelledError) as caught,
    ):
        send(
            EmailMultiAlternatives(subject="Forecast", body="Saved", to=["to@example.com"]),
            before_send=check,
        )
    assert smtp.accepted == 0
    assert smtp.closed
    if isinstance(caught.value, NotSentError):
        assert caught.value.code == "mail_preparation_failed"


def test_configured_mailer_lookups_create_independent_backends_without_connecting() -> None:
    barrier = Barrier(2)

    def inspect_backend() -> tuple[int, EmailBackend, EmailBackend]:
        first, second = mail.mailers["default"], mail.mailers["default"]
        assert isinstance(first, EmailBackend)
        assert isinstance(second, EmailBackend)
        barrier.wait(timeout=10)
        return get_ident(), first, second

    with (
        override_settings(
            MAILERS={
                "default": {
                    "BACKEND": "pyracast.mail.smtp.EmailBackend",
                    "OPTIONS": {"host": "localhost", "timeout": 15},
                }
            }
        ),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        results = list(pool.map(lambda _: inspect_backend(), range(2)))
    assert len({thread for thread, _, _ in results}) == 2
    assert len({id(backend) for _, first, second in results for backend in (first, second)}) == 4
    assert all(backend.connection is None for _, *backends in results for backend in backends)
