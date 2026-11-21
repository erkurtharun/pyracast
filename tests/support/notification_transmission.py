from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import ClassVar

from django.core.mail import EmailMessage
from django.core.mail.backends.base import BaseEmailBackend
from django.db import connection
from django.test import override_settings

BACKEND = "tests.support.notification_transmission.OpeningBackend"


class OpeningBackend(BaseEmailBackend):
    """Records what it is asked to send; `action` runs as the connection opens and `reply`
    answers each message in place of the mail server, raising to refuse it."""

    action: ClassVar[Callable[[], None] | None] = None
    reply: ClassVar[Callable[[EmailMessage], None] | None] = None
    sent: ClassVar[list[EmailMessage]] = []

    def open(self) -> bool:
        assert not connection.in_atomic_block
        action = type(self).action
        if action is not None:
            action()
        return True

    def send_messages(self, email_messages: Sequence[EmailMessage] | None) -> int:
        assert not connection.in_atomic_block
        assert email_messages is not None
        reply = type(self).reply
        for message in email_messages:
            if reply is not None:
                reply(message)
            self.sent.append(message)
        return len(email_messages)


@contextmanager
def replying(reply: Callable[[EmailMessage], None]) -> Iterator[list[EmailMessage]]:
    OpeningBackend.action, OpeningBackend.reply, OpeningBackend.sent = None, reply, []
    try:
        with override_settings(MAILERS={"default": {"BACKEND": BACKEND}}):
            yield OpeningBackend.sent
    finally:
        OpeningBackend.reply = None
