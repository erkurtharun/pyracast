from collections.abc import Callable, Sequence
from typing import ClassVar

from django.core.mail import EmailMessage
from django.core.mail.backends.base import BaseEmailBackend
from django.db import connection


class OpeningBackend(BaseEmailBackend):
    action: ClassVar[Callable[[], None] | None] = None
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
        self.sent.extend(email_messages)
        return len(email_messages)
