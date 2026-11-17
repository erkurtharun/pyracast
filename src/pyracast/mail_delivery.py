from email.message import EmailMessage
from email.policy import EmailPolicy
from typing import TYPE_CHECKING, Any


class NotSentError(Exception):
    """The adapter guarantees that the message was not accepted for delivery."""

    def __init__(self, message: str = "", *, code: str = "mail_not_sent") -> None:
        super().__init__(message)
        self.code = code


class PermanentlyNotSentError(NotSentError):
    """The mail service rejected delivery without a retryable response."""


class DeliveryUncertainError(Exception):
    """Transmission may have succeeded; retrying can duplicate delivery."""


if TYPE_CHECKING:
    _Policy = EmailPolicy[EmailMessage]
else:
    _Policy = EmailPolicy


# Folding a long MIME type produces encoded-words, which RFC 2047 section 5 forbids in Content-Type.
class WirePolicy(_Policy):
    def _unfolded(self) -> EmailPolicy[EmailMessage]:
        return EmailPolicy(linesep=self.linesep, cte_type=self.cte_type, max_line_length=0)

    def fold_binary(self, name: str, value: Any) -> bytes:
        if name.lower() == "content-type":
            return self._unfolded().fold_binary(name, value)
        return super().fold_binary(name, value)


def wire_policy(*, eight_bit: bool) -> WirePolicy:
    return WirePolicy(linesep="\r\n", cte_type="8bit" if eight_bit else "7bit")
