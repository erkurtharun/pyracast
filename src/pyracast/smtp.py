import smtplib
from typing import cast

from django.core.mail import EmailMessage
from django.core.mail.backends.smtp import EmailBackend as SMTPEmailBackend

from pyracast.mail_delivery import wire_policy


class EmailBackend(SMTPEmailBackend):
    def _send(self, email_message: EmailMessage) -> bool:
        if not email_message.recipients():
            return False
        sender = self.prep_address(email_message.from_email)
        recipients = [self.prep_address(address) for address in email_message.recipients()]
        connection = cast(smtplib.SMTP, self.connection)
        try:
            connection.ehlo_or_helo_if_needed()
            eight_bit = connection.has_extn("8bitmime")
            content = email_message.message(policy=wire_policy(eight_bit=eight_bit)).as_bytes()
            options = [f"size={len(content)}"] if connection.has_extn("size") else []
            if eight_bit:
                options.append("BODY=8BITMIME")
            code, response = connection.mail(sender, options)
            if code != 250:
                self._reset_transaction(connection, code)
                raise smtplib.SMTPSenderRefused(code, response, sender)
            refused: dict[str, tuple[int, bytes]] = {}
            for recipient in recipients:
                code, response = connection.rcpt(recipient)
                if code not in {250, 251}:
                    refused[recipient] = code, response
                if code == 421:
                    break
            if refused:
                # No DATA is transmitted when any envelope recipient is rejected.
                self._reset_transaction(connection, code)
                raise smtplib.SMTPRecipientsRefused(refused)
            code, response = connection.data(content)
            if code != 250:
                self._reset_transaction(connection, code)
                raise smtplib.SMTPDataError(code, response)
        except smtplib.SMTPException:
            if not self.fail_silently:
                raise
            return False
        return True

    def _reset_transaction(self, connection: smtplib.SMTP, code: int) -> None:
        if code != 421:
            try:
                if connection.rset()[0] == 250:
                    return
            except OSError:
                pass
        connection.close()
        self.connection = None
