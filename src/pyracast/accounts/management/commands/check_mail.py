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

from pyracast.notifications.mail import XLSX_MIME, send_message


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


class Command(BaseCommand):
    help = "Send one reminder-shaped test mail through the configured mail backend."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--send-to", required=True)

    def handle(self, *args: Any, **options: Any) -> None:
        recipient = options["send_to"].strip()
        try:
            validate_email(recipient)
        except ValidationError:
            raise CommandError("--send-to must be a valid email address.") from None
        send_message(sample_message(recipient))
        self.stdout.write("The mail service accepted the test mail.")
