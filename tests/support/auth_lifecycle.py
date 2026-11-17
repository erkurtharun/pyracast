import base64
import hmac
import re
from typing import Any

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client

AUTH = "/api/auth/browser/v1/"


PASSWORD = "test-only-" + "falcon-cloud-79!"


def member() -> User:
    user = User.objects.create_user(
        username="member", email="member@example.com", password=PASSWORD
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


def submit(client: Client, path: str, data: dict[str, Any]) -> Any:
    token = client.get("/api/session/").json()["csrf_token"]
    return client.post(AUTH + path, data, content_type="application/json", HTTP_X_CSRFTOKEN=token)


def mailed_code() -> str:
    for message in reversed(mail.outbox):
        for line in str(message.body).splitlines():
            if re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", line.strip()):
                return line.strip()
    raise AssertionError("No authentication code was delivered.")


def totp_code(secret: str, now: float) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.digest(key, (int(now) // 30).to_bytes(8), "sha1")
    offset = digest[-1] & 15
    value = int.from_bytes(digest[offset : offset + 4]) & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"
