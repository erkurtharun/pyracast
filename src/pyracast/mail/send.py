from collections.abc import Callable
from contextlib import suppress

from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMessage
from django.core.mail.backends.base import BaseEmailBackend

from pyracast.mail.errors import MailFailure, NotSentError, SendCancelledError


def configured_mailer() -> BaseEmailBackend:
    try:
        return mail.mailers["default"]
    except ImproperlyConfigured, ValueError, TypeError:
        raise NotSentError(
            "The mail backend configuration is invalid.", code=MailFailure.CONFIGURATION_INVALID
        ) from None


def probe() -> None:
    """Open and close the configured transport without sending, or raise a typed mail error."""
    backend = configured_mailer()
    try:
        backend.fail_silently = False
        backend.open()
    finally:
        with suppress(OSError):
            backend.close()


def send(message: EmailMessage, *, before_send: Callable[[], bool] | None = None) -> None:
    """Send one message or raise a typed mail error.

    ``before_send`` runs after the connection opens and before the mail transaction; returning False
    raises ``SendCancelledError`` and nothing is transmitted.
    """
    if not message.recipients():
        raise NotSentError("The message has no recipient.")
    backend = configured_mailer()
    try:
        backend.fail_silently = False
        backend.open()
        if before_send is not None:
            try:
                permitted = before_send()
            except Exception:
                raise NotSentError(
                    "Transmission authority could not be confirmed.",
                    code=MailFailure.PREPARATION_FAILED,
                ) from None
            if not permitted:
                raise SendCancelledError
        if backend.send_messages([message]) != 1:
            raise NotSentError("The mail backend did not send the message.")
    finally:
        # QUIT cannot revoke an already acknowledged DATA response.
        with suppress(OSError):
            backend.close()
