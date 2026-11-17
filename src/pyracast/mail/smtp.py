import smtplib
from collections.abc import Sequence
from typing import cast

from django.core.mail import EmailMessage
from django.core.mail.backends.smtp import EmailBackend as SMTPEmailBackend

from pyracast.mail.errors import (
    DeliveryUncertainError,
    MailFailure,
    NotSentError,
    PermanentlyNotSentError,
)
from pyracast.mail.policy import wire_policy


def _rejection(code: int) -> NotSentError:
    if 500 <= code < 600:
        return PermanentlyNotSentError("The mail server rejected the message.")
    return NotSentError("The mail server did not accept the message.")


class EmailBackend(SMTPEmailBackend):
    def open(self) -> bool | None:
        try:
            return super().open()
        except smtplib.SMTPAuthenticationError as error:
            code = (
                MailFailure.NOT_SENT
                if 400 <= error.smtp_code < 500
                else MailFailure.CREDENTIALS_REJECTED
            )
            raise NotSentError(
                "The mail server did not authenticate the connection.", code=code
            ) from None
        except OSError:
            raise NotSentError("The mail connection could not be established.") from None

    def send_messages(self, email_messages: Sequence[EmailMessage] | None) -> int:
        if not email_messages:
            return 0
        created = self.open()
        if self.connection is None:
            return 0
        sent = 0
        try:
            for message in email_messages:
                if not message.recipients():
                    continue
                try:
                    self._transmit(message)
                except NotSentError, DeliveryUncertainError:
                    if not self.fail_silently:
                        raise
                else:
                    sent += 1
        finally:
            if created:
                self.close()
        return sent

    def _transmit(self, message: EmailMessage) -> None:
        connection = cast(smtplib.SMTP, self.connection)
        try:
            sender = self.prep_address(message.from_email)
            recipients = [self.prep_address(address) for address in message.recipients()]
        except ValueError:
            raise PermanentlyNotSentError("The message addresses are invalid.") from None
        try:
            connection.ehlo_or_helo_if_needed()
            eight_bit = connection.has_extn("8bitmime")
            content = message.message(policy=wire_policy(eight_bit=eight_bit)).as_bytes()
            options = [f"size={len(content)}"] if connection.has_extn("size") else []
            if eight_bit:
                options.append("BODY=8BITMIME")
            code, _response = connection.mail(sender, options)
            if code != 250:
                self._reset_transaction(connection, code)
                raise _rejection(code)
            self._address(connection, recipients)
        except smtplib.SMTPNotSupportedError:
            raise PermanentlyNotSentError("The mail transaction is unsupported.") from None
        except ValueError:
            raise PermanentlyNotSentError("The message headers or content are invalid.") from None
        except OSError:
            raise NotSentError("The mail transaction failed before transmission.") from None
        try:
            code, _response = connection.data(content)
        except smtplib.SMTPDataError as error:
            raise _rejection(error.smtp_code) from None
        except OSError:
            raise DeliveryUncertainError(
                "Acceptance of the message could not be confirmed."
            ) from None
        if code != 250:
            self._reset_transaction(connection, code)
            if not 400 <= code < 600:
                raise DeliveryUncertainError("The mail server returned an invalid final reply.")
            raise _rejection(code)

    def _address(self, connection: smtplib.SMTP, recipients: list[str]) -> None:
        codes: list[int] = []
        for recipient in recipients:
            code, _response = connection.rcpt(recipient)
            if code not in {250, 251}:
                codes.append(code)
            if code == 421:
                break
        if codes:
            # No DATA is transmitted when any envelope recipient is rejected.
            self._reset_transaction(connection, codes[-1])
            raise _rejection(550 if all(500 <= code < 600 for code in codes) else 450)

    def _reset_transaction(self, connection: smtplib.SMTP, code: int) -> None:
        if code != 421:
            try:
                if connection.rset()[0] == 250:
                    return
            except OSError:
                pass
        connection.close()
        self.connection = None
