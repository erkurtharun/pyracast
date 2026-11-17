import base64
import json
import re
from collections.abc import Callable, Iterator
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from io import BytesIO, StringIO
from typing import Any, cast

import httpx
import pytest
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.core.management import call_command
from django.test import override_settings
from openpyxl import load_workbook

from pyracast.mail.gmail import SEND_SCOPE, TOKEN_URL
from pyracast.mail.send import send
from tests.smtp_server import Script, loopback_smtp

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SUBJECT = (
    "Çanakkale Güneş Enerjisi Santrali — 18 Kas\u0131m 2026 tarihli üretim tahmini ve ölçüm özeti"
)
BODY = "Günayd\u0131n. Yar\u0131n\u0131n üretim tahmini ektedir: ğüş\u0131öç ĞÜŞİÖÇ"
FILENAME = "tahmin-Çanakkale-2026-11-18.xlsx"
SENDER = "Pyracast <info@pyracast.app>"
ENCODED_WORD = re.compile(rb"=\?[^?\s]+\?[bBqQ]\?[^?\s]*\?=")
Transmit = Callable[[EmailMultiAlternatives], bytes]


def reminder() -> EmailMultiAlternatives:
    message = EmailMultiAlternatives(SUBJECT, BODY, SENDER, bcc=["customer@example.com"])
    message.attach_alternative(f"<p>{BODY}</p>", "text/html")
    message.attach(FILENAME, b"PK\x03\x04 workbook", XLSX)
    return message


def smtp_options(port: int) -> dict[str, Any]:
    return {"host": "127.0.0.1", "port": port, "use_tls": False, "timeout": 5}


@pytest.fixture
def smtp_wire() -> Iterator[tuple[Script, Transmit]]:
    script = Script()

    def transmit(message: EmailMultiAlternatives) -> bytes:
        send(message)
        assert len(script.transactions) == 1
        return script.transactions[0].data

    with (
        loopback_smtp(script) as port,
        override_settings(
            MAILERS={
                "default": {
                    "BACKEND": "pyracast.mail.smtp.EmailBackend",
                    "OPTIONS": smtp_options(port),
                }
            }
        ),
    ):
        yield script, transmit


@pytest.fixture
def gmail_wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[Transmit]:
    sent: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(
                200, json={"access_token": "access", "token_type": "Bearer", "scope": SEND_SCOPE}
            )
        sent.append(base64.urlsafe_b64decode(json.loads(request.content)["raw"]))
        return httpx.Response(200, json={"id": "accepted"})

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )

    def transmit(message: EmailMultiAlternatives) -> bytes:
        send(message)
        assert len(sent) == 1
        return sent[0]

    options = {"client_id": "client", "client_secret": "secret", "refresh_token": "refresh"}
    with override_settings(
        DEFAULT_FROM_EMAIL=SENDER,
        MAILERS={"default": {"BACKEND": "pyracast.mail.gmail.EmailBackend", "OPTIONS": options}},
    ):
        yield transmit


@pytest.fixture(params=["smtp", "gmail"])
def transmit(request: pytest.FixtureRequest) -> Transmit:
    if request.param == "smtp":
        return cast(tuple[Script, Transmit], request.getfixturevalue("smtp_wire"))[1]
    return cast(Transmit, request.getfixturevalue("gmail_wire"))


def parsed(raw: bytes) -> EmailMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(message, EmailMessage)
    return message


def text(message: EmailMessage, kind: str) -> str:
    body = message.get_body((kind,))
    assert isinstance(body, EmailMessage)
    return str(body.get_content())


def attachment(message: EmailMessage) -> EmailMessage:
    (part,) = (part for part in message.iter_attachments() if isinstance(part, EmailMessage))
    return part


def test_attachment_type_is_one_unfolded_header_line(transmit: Transmit) -> None:
    raw = transmit(reminder())

    content_types = [
        line for line in raw.split(b"\r\n") if line.lower().startswith(b"content-type:")
    ]
    assert f"Content-Type: {XLSX}".encode() in content_types
    assert not any(ENCODED_WORD.search(line) for line in content_types)
    assert attachment(parsed(raw)).get_content_type() == XLSX


def test_long_turkish_subject_uses_short_encoded_words(transmit: Transmit) -> None:
    raw = transmit(reminder())

    header = raw.split(b"\r\n\r\n", 1)[0]
    words = ENCODED_WORD.findall(header)
    assert words
    assert max(len(word) for word in words) <= 75
    assert str(parsed(raw)["Subject"]) == SUBJECT
    assert max(len(line) for line in raw.split(b"\r\n")) <= 998


def test_non_ascii_attachment_name_keeps_its_rfc_2231_form(transmit: Transmit) -> None:
    raw = transmit(reminder())

    assert b"filename*=utf-8''tahmin-%C3%87anakkale-2026-11-18.xlsx" in raw
    assert attachment(parsed(raw)).get_filename() == FILENAME


def test_advertised_8bitmime_is_declared_for_an_8bit_body(
    smtp_wire: tuple[Script, Transmit],
) -> None:
    script, transmit = smtp_wire

    raw = transmit(reminder())

    assert script.transactions[0].mail_from.upper().endswith(" BODY=8BITMIME")
    assert any(byte > 0x7F for byte in raw)
    assert text(parsed(raw), "plain").strip() == BODY


def test_server_without_8bitmime_receives_a_7bit_message(
    smtp_wire: tuple[Script, Transmit],
) -> None:
    script, transmit = smtp_wire
    script.extensions = ["SIZE 10000000"]

    raw = transmit(reminder())

    assert "BODY=" not in script.transactions[0].mail_from.upper()
    assert all(byte <= 0x7F for byte in raw)
    message = parsed(raw)
    assert text(message, "plain").strip() == BODY
    assert BODY in text(message, "html")
    assert attachment(message).get_content() == b"PK\x03\x04 workbook"


@override_settings(
    DEFAULT_FROM_EMAIL=SENDER,
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
)
def test_check_mail_sends_one_reminder_shaped_test_mail() -> None:
    output = StringIO()

    call_command("check_mail", "--send-to", "operator@example.com", stdout=output)

    (message,) = mail.outbox
    assert isinstance(message, EmailMultiAlternatives)
    assert message.to == ["operator@example.com"]
    assert message.from_email == SENDER
    assert message.alternatives[0][1] == "text/html"
    ((name, content, mimetype),) = message.attachments
    assert mimetype == XLSX
    assert str(name).endswith(".xlsx")
    assert isinstance(content, bytes)
    assert load_workbook(BytesIO(content)).active is not None
    assert "operator@example.com" not in output.getvalue()


def test_check_mail_test_mail_is_valid_on_the_wire(smtp_wire: tuple[Script, Transmit]) -> None:
    script, _transmit = smtp_wire

    call_command("check_mail", "--send-to", "operator@example.com", stdout=StringIO())

    (transaction,) = script.transactions
    assert transaction.recipients == ["operator@example.com"]
    assert f"Content-Type: {XLSX}".encode() in transaction.data.split(b"\r\n")
