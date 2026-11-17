import re
from typing import Any

from django.core import mail
from django.test import Client

PASSWORD = "test-only-" + "falcon-cloud-79!"


DETAILS = {"company_name": "Sunfield Energy", "email": "Owner@Example.COM", "password": PASSWORD}


AUTH = "/api/auth/browser/v1/"


def mailed_code() -> str:
    for message in reversed(mail.outbox):
        for line in str(message.body).splitlines():
            if re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", line.strip()):
                return line.strip()
    raise AssertionError("No authentication code was delivered.")


def signup(client: Client, overrides: dict[str, Any] | None = None) -> Any:
    token = client.get("/api/session/").json()["csrf_token"]
    return client.post(
        AUTH + "auth/signup",
        DETAILS | (overrides or {}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
