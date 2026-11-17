from enum import StrEnum


class MailFailure(StrEnum):
    NOT_SENT = "mail_not_sent"
    CONFIGURATION_INVALID = "mail_configuration_invalid"
    CREDENTIALS_REJECTED = "mail_credentials_rejected"
    AUTHORIZATION_REVOKED = "mail_authorization_revoked"
    RATE_LIMITED = "mail_rate_limited"
    REJECTED = "mail_rejected"
    SENDER_FORBIDDEN = "mail_sender_forbidden"
    PREPARATION_FAILED = "mail_preparation_failed"
    OUTCOME_UNCERTAIN = "mail_outcome_uncertain"


CREDENTIAL_FAILURES = (MailFailure.CREDENTIALS_REJECTED, MailFailure.AUTHORIZATION_REVOKED)


class NotSentError(Exception):
    """The transport guarantees that the message was not accepted for delivery."""

    default_code = MailFailure.NOT_SENT

    def __init__(self, message: str = "", *, code: MailFailure | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code


class PermanentlyNotSentError(NotSentError):
    """Retrying cannot succeed without a configuration or content change."""

    default_code = MailFailure.REJECTED


class DeliveryUncertainError(Exception):
    """Transmission may have succeeded; retrying can duplicate delivery."""

    code = MailFailure.OUTCOME_UNCERTAIN


class SendCancelledError(Exception):
    """The caller withdrew transmission authority before the mail transaction began."""
