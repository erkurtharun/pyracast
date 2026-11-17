import base64
from collections.abc import Sequence
from contextlib import suppress
from email.utils import parseaddr
from typing import Any
from urllib.parse import quote

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.mail import EmailMessage
from django.core.mail.backends.base import BaseEmailBackend
from django.core.validators import validate_email

from pyracast.mail_delivery import (
    DeliveryUncertainError,
    NotSentError,
    PermanentlyNotSentError,
    wire_policy,
)

SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105


class EmailBackend(BaseEmailBackend):
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        sender: str = "",
        timeout: float = 15,
        fail_silently: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.fail_silently = fail_silently
        self.client_id, self.client_secret, self.refresh_token = (
            client_id,
            client_secret,
            refresh_token,
        )
        self.sender = parseaddr(sender or settings.DEFAULT_FROM_EMAIL)[1]
        try:
            validate_email(self.sender)
        except ValidationError:
            raise ImproperlyConfigured(
                "Gmail requires a valid authorized sender mailbox."
            ) from None
        if not all((client_id, client_secret, refresh_token)) or timeout <= 0:
            raise ImproperlyConfigured(
                "Gmail requires OAuth client credentials and a positive timeout."
            )
        self.timeout = timeout
        self._client: httpx.Client | None = None
        self._access_token = ""

    def open(self) -> bool:
        if self._client is not None:
            return False
        client = httpx.Client(timeout=self.timeout, trust_env=False, follow_redirects=False)
        try:
            response = client.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            if response.status_code != 200:
                raise ValueError
            token = response.json()
            if (
                not isinstance(token, dict)
                or not isinstance(token.get("access_token"), str)
                or not token["access_token"]
                or token.get("token_type", "").lower() != "bearer"
                or not isinstance(token.get("scope"), str)
                or set(token["scope"].split()) != {SEND_SCOPE}
            ):
                raise ValueError
        except httpx.HTTPError, ValueError, TypeError, AttributeError:
            with suppress(httpx.HTTPError, OSError):
                client.close()
            if not self.fail_silently:
                raise NotSentError(
                    "Gmail OAuth authentication failed before transmission.",
                    code="mail_authentication_failed",
                ) from None
            return False
        self._client, self._access_token = client, token["access_token"]
        return True

    def close(self) -> None:
        client, self._client = self._client, None
        self._access_token = ""
        if client is not None:
            with suppress(httpx.HTTPError, OSError):
                client.close()

    def send_messages(self, email_messages: Sequence[EmailMessage] | None) -> int:
        if not email_messages:
            return 0
        created = self.open()
        if self._client is None:
            return 0
        count = 0
        try:
            for message in email_messages:
                if not message.recipients():
                    continue
                try:
                    self._send(message)
                except NotSentError, DeliveryUncertainError:
                    if not self.fail_silently:
                        raise
                else:
                    count += 1
        finally:
            if created:
                self.close()
        return count

    def _send(self, message: EmailMessage) -> None:
        try:
            mime = message.message(policy=wire_policy(eight_bit=True))
            if parseaddr(str(mime["From"]))[1].casefold() != self.sender.casefold():
                raise ValueError
            # Gmail routes using MIME headers; Django normally omits envelope-only Bcc recipients.
            if message.bcc:
                mime["Bcc"] = ", ".join(message.bcc)
            raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
        except ValueError, TypeError:
            raise PermanentlyNotSentError("Gmail message headers or content are invalid.") from None
        assert self._client is not None  # noqa: S101 -- open() precedes every transmission.
        try:
            response = self._client.post(
                "https://gmail.googleapis.com/gmail/v1/users/"
                f"{quote(self.sender, safe='')}/messages/send",
                headers={"Authorization": f"Bearer {self._access_token}"},
                json={"raw": raw},
            )
        except httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout:
            raise NotSentError("The Gmail connection could not be established.") from None
        except httpx.HTTPError:
            raise DeliveryUncertainError("Gmail acceptance could not be confirmed.") from None
        if response.status_code == 401:
            raise NotSentError("Gmail rejected authentication.", code="mail_authentication_failed")
        if response.status_code == 429 or self._quota_rejection(response):
            raise NotSentError("Gmail rejected the request due to quota.", code="mail_rate_limited")
        if 400 <= response.status_code < 500 and response.status_code != 408:
            raise PermanentlyNotSentError("Gmail rejected the message.")
        try:
            accepted = response.json()
        except ValueError:
            accepted = None
        if (
            response.status_code != 200
            or not isinstance(accepted, dict)
            or not isinstance(accepted.get("id"), str)
            or not accepted["id"]
        ):
            raise DeliveryUncertainError("Gmail acceptance could not be confirmed.")

    @staticmethod
    def _quota_rejection(response: httpx.Response) -> bool:
        if response.status_code != 403:
            return False
        try:
            errors = response.json()["error"]["errors"]
            reasons = {error["reason"] for error in errors}
        except ValueError, KeyError, TypeError:
            return False
        return bool(reasons) and reasons <= {
            "rateLimitExceeded",
            "userRateLimitExceeded",
            "dailyLimitExceeded",
        }
