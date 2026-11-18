from io import BytesIO
from typing import Any, cast

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.core.validators import validate_email
from django.template.loader import render_to_string
from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from pyracast.mail.errors import DeliveryUncertainError, MailFailure, NotSentError
from pyracast.mail.send import probe, send
from pyracast.notifications.mail import XLSX_MIME


def sample_workbook() -> bytes:
    workbook = Workbook()
    sheet = cast(Worksheet, workbook.active)
    sheet.title = "Pyracast"
    sheet["A1"] = "Pyracast mail test"
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def sample_message(recipient: str) -> EmailMultiAlternatives:
    body = render_to_string("mail/test_message.txt").strip()
    message = EmailMultiAlternatives(
        "Pyracast mail test", body, settings.DEFAULT_FROM_EMAIL, [recipient]
    )
    message.attach_alternative(render_to_string("mail/test_message.html"), "text/html")
    message.attach("pyracast-mail-test.xlsx", sample_workbook(), XLSX_MIME)
    return message


# Operators and the release script read these exit codes; docs/DEPLOYMENT.md lists them.
EXIT_CODES = {
    MailFailure.CONFIGURATION_INVALID: 2,
    MailFailure.CREDENTIALS_REJECTED: 3,
    MailFailure.AUTHORIZATION_REVOKED: 4,
    MailFailure.SENDER_FORBIDDEN: 5,
    MailFailure.REJECTED: 6,
    MailFailure.RATE_LIMITED: 7,
    MailFailure.NOT_SENT: 8,
    MailFailure.OUTCOME_UNCERTAIN: 9,
    MailFailure.PREPARATION_FAILED: 10,
}


class Command(BaseCommand):
    help = (
        "Open the configured mail transport without sending; with --send-to, send one "
        "reminder-shaped test mail. Exits with a code per mail failure."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--send-to")

    def handle(self, *args: Any, **options: Any) -> None:
        recipient = options["send_to"]
        if recipient is not None:
            recipient = recipient.strip()
            try:
                validate_email(recipient)
            except ValidationError:
                raise CommandError("--send-to must be a valid email address.") from None
        try:
            if recipient is None:
                probe()
            else:
                send(sample_message(recipient))
        except (NotSentError, DeliveryUncertainError) as error:
            raise CommandError(
                f"Mail check failed: {error.code}", returncode=EXIT_CODES[error.code]
            ) from None
        self.stdout.write(
            "The mail service accepted the connection."
            if recipient is None
            else "The mail service accepted the test mail."
        )
