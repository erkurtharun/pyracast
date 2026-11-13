class NotSentError(Exception):
    """The adapter guarantees that the message was not accepted for delivery."""

    def __init__(self, message: str = "", *, code: str = "mail_not_sent") -> None:
        super().__init__(message)
        self.code = code


class PermanentlyNotSentError(NotSentError):
    """The mail service rejected delivery without a retryable response."""


class DeliveryUncertainError(Exception):
    """Transmission may have succeeded; retrying can duplicate delivery."""
