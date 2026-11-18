import base64
import json
from collections.abc import Callable, Iterator
from email import policy
from email.parser import BytesParser
from pathlib import Path
from runpy import run_path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMultiAlternatives
from django.test import Client, override_settings

from pyracast import settings as project_settings
from pyracast.accounts.checks import check_authentication_mail_delivery
from pyracast.mail.errors import (
    DeliveryUncertainError,
    NotSentError,
    PermanentlyNotSentError,
    SendCancelledError,
)
from pyracast.mail.gmail import SEND_SCOPE, TOKEN_URL, EmailBackend
from pyracast.mail.send import send
from pyracast.notifications.jobs import process_notification
from tests.support.company_registration import signup
from tests.support.notifications import SLOT, ready

SENDER = "Pyracast <info@pyracast.app>"
OPTIONS = {
    "client_id": "gmail-client",
    "client_secret": "gmail-secret",
    "refresh_token": "gmail-refresh",
}
MAILERS = {"default": {"BACKEND": "pyracast.mail.gmail.EmailBackend", "OPTIONS": OPTIONS}}


@pytest.fixture(autouse=True)
def gmail_sender() -> Iterator[None]:
    with override_settings(DEFAULT_FROM_EMAIL=SENDER):
        yield


@pytest.fixture
def gmail_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Callable[[httpx.Request], httpx.Response]], None]:
    client = httpx.Client

    def install(respond: Callable[[httpx.Request], httpx.Response]) -> None:
        def make_client(**kwargs: Any) -> httpx.Client:
            return client(transport=httpx.MockTransport(respond), **kwargs)

        monkeypatch.setattr(httpx, "Client", make_client)

    return install


def token() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "gmail-access",
            "token_type": "Bearer",
            "scope": SEND_SCOPE,
        },
    )


def message() -> EmailMultiAlternatives:
    return EmailMultiAlternatives(
        "Pyracast forecast",
        "Plain forecast",
        SENDER,
        ["customer@example.com"],
        headers={"Message-ID": "<delivery-123@notifications.pyracast>"},
    )


@pytest.mark.parametrize("blind_only", [False, True])
def test_gmail_serializes_recipients_html_csv_and_stable_message_id(
    gmail_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    blind_only: bool,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return (
            token()
            if str(request.url) == TOKEN_URL
            else httpx.Response(200, json={"id": "accepted"})
        )

    gmail_transport(respond)
    email = message()
    email.cc, email.bcc = ["copy@example.com"], ["blind@example.com"]
    if blind_only:
        email.to, email.cc = [], []
        email.bcc = ["first@example.com", "second@example.com"]
    email.reply_to = ["reply@pyracast.app"]
    email.attach_alternative("<p>Forecast</p>", "text/html")
    email.attach("forecast.csv", "date,energy_kwh\n2026-11-13,45\n", "text/csv")
    with override_settings(MAILERS=MAILERS):
        first, second = mail.mailers["default"], mail.mailers["default"]
        assert first is not second
        send(email)
    assert len(requests) == 2
    assert parse_qs(requests[0].content.decode()) == {
        "client_id": [OPTIONS["client_id"]],
        "client_secret": [OPTIONS["client_secret"]],
        "refresh_token": [OPTIONS["refresh_token"]],
        "grant_type": ["refresh_token"],
    }
    assert str(requests[1].url) == (
        "https://gmail.googleapis.com/gmail/v1/users/info%40pyracast.app/messages/send"
    )
    assert requests[1].headers["Authorization"] == "Bearer gmail-access"
    raw = base64.urlsafe_b64decode(json.loads(requests[1].content)["raw"])
    mime = BytesParser(policy=policy.default).parsebytes(raw)
    assert str(mime["From"]) == SENDER
    assert str(mime["MIME-Version"]) == "1.0"
    if blind_only:
        assert mime["To"] is None
        assert mime["Cc"] is None
        assert str(mime["Bcc"]) == "first@example.com, second@example.com"
    else:
        assert str(mime["To"]) == "customer@example.com"
        assert str(mime["Cc"]) == "copy@example.com"
        assert str(mime["Bcc"]) == "blind@example.com"
    assert str(mime["Reply-To"]) == "reply@pyracast.app"
    assert str(mime["Message-ID"]) == "<delivery-123@notifications.pyracast>"
    parts = {part.get_content_type(): part for part in mime.walk() if not part.is_multipart()}
    assert parts["text/plain"].get_content().strip() == "Plain forecast"
    assert parts["text/html"].get_content().strip() == "<p>Forecast</p>"
    assert parts["text/csv"].get_filename() == "forecast.csv"
    assert "2026-11-13,45" in parts["text/csv"].get_content()


@pytest.mark.parametrize(
    "scope", ["", "openid", SEND_SCOPE + " https://www.googleapis.com/auth/gmail.readonly"]
)
def test_gmail_rejects_missing_or_broader_scopes_before_transmission(
    gmail_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    scope: str,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"access_token": "private-token", "token_type": "Bearer", "scope": scope}
        )

    gmail_transport(respond)
    with override_settings(MAILERS=MAILERS), pytest.raises(NotSentError) as error:
        send(message())
    assert error.value.code == "mail_credentials_rejected"
    assert "private" not in str(error.value)
    assert len(requests) == 1


def test_gmail_authenticates_before_rechecking_transmission_permission(
    gmail_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return token()

    def permission() -> bool:
        assert len(requests) == 1
        assert str(requests[0].url) == TOKEN_URL
        return False

    gmail_transport(respond)
    with override_settings(MAILERS=MAILERS), pytest.raises(SendCancelledError):
        send(message(), before_send=permission)
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (429, "mail_rate_limited"),
        (500, "mail_not_sent"),
        (503, "mail_not_sent"),
        (599, "mail_not_sent"),
    ],
)
@pytest.mark.parametrize("json_body", [False, True])
def test_oauth_transient_failure_preserves_credentials_without_sending(
    gmail_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    status: int,
    code: str,
    json_body: bool,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if json_body:
            return httpx.Response(status, json={"error": "private-provider-error"})
        return httpx.Response(status, text="private-provider-error")

    gmail_transport(respond)
    with override_settings(MAILERS=MAILERS), pytest.raises(NotSentError) as error:
        send(message())
    assert error.value.code == code
    assert "private" not in str(error.value)
    assert [str(request.url) for request in requests] == [TOKEN_URL]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("mode", "status", "code"),
    [
        ("accepted", "sent", ""),
        ("auth", "retry", "mail_credentials_rejected"),
        ("token_rejected", "retry", "mail_credentials_rejected"),
        ("token_revoked", "failed", "mail_authorization_revoked"),
        ("token_quota", "retry", "mail_rate_limited"),
        ("token_server", "retry", "mail_not_sent"),
        ("quota", "retry", "mail_rate_limited"),
        ("quota403", "retry", "mail_rate_limited"),
        ("open_timeout", "retry", "mail_not_sent"),
        ("connect_timeout", "retry", "mail_not_sent"),
        ("rejected", "failed", "mail_rejected"),
        ("forbidden", "failed", "mail_sender_forbidden"),
        ("server", "uncertain", "mail_outcome_uncertain"),
        ("read_timeout", "uncertain", "mail_outcome_uncertain"),
        ("write_timeout", "uncertain", "mail_outcome_uncertain"),
        ("invalid_ack", "uncertain", "mail_outcome_uncertain"),
    ],
)
def test_gmail_preserves_durable_delivery_acceptance_and_retry_policy(
    gmail_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    mode: str,
    status: str,
    code: str,
) -> None:
    sends = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        if str(request.url) == TOKEN_URL:
            if mode == "open_timeout":
                raise httpx.ReadTimeout("private-token", request=request)
            if mode == "token_rejected":
                return httpx.Response(401, json={"error": "invalid_client"})
            if mode == "token_revoked":
                return httpx.Response(400, json={"error": "invalid_grant"})
            if mode == "token_quota":
                return httpx.Response(429)
            if mode == "token_server":
                return httpx.Response(503)
            return token()
        sends += 1
        if mode.endswith("timeout"):
            failure = {
                "connect_timeout": httpx.ConnectTimeout,
                "read_timeout": httpx.ReadTimeout,
                "write_timeout": httpx.WriteTimeout,
            }[mode]
            raise failure("private-token", request=request)
        if mode == "forbidden":
            return httpx.Response(403, json={"error": {"errors": [{"reason": "forbidden"}]}})
        if mode == "quota403":
            return httpx.Response(
                403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}
            )
        response_status = {"auth": 401, "quota": 429, "rejected": 400, "server": 500}.get(mode, 200)
        return httpx.Response(
            response_status, json={} if mode == "invalid_ack" else {"id": "accepted"}
        )

    gmail_transport(respond)
    delivery = ready()
    with override_settings(MAILERS=MAILERS, DEFAULT_FROM_EMAIL=SENDER):
        assert process_notification(delivery.pk, SLOT) is (status == "sent")
        delivery.refresh_from_db()
        assert delivery.status == status
        assert delivery.last_error == code
        if status == "uncertain":
            assert not process_notification(delivery.pk, SLOT)
    assert sends == (0 if mode == "open_timeout" or mode.startswith("token_") else 1)


def test_gmail_fail_silently_counts_only_acknowledged_messages(
    gmail_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
) -> None:
    responses = [token(), httpx.Response(400), httpx.Response(200, json={"id": "accepted"})]
    gmail_transport(lambda _request: responses.pop(0))
    backend = EmailBackend(
        client_id=OPTIONS["client_id"],
        client_secret=OPTIONS["client_secret"],
        refresh_token=OPTIONS["refresh_token"],
        fail_silently=True,
    )
    assert backend.send_messages([message(), message()]) == 1
    assert backend.send_messages([]) == 0
    backend.close()


def test_gmail_configuration_accepts_oauth_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYRACAST_EMAIL_BACKEND", "gmail")
    monkeypatch.setenv("PYRACAST_FROM_EMAIL", SENDER)
    for suffix in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN"):
        monkeypatch.setenv("PYRACAST_GMAIL_" + suffix, OPTIONS[suffix.lower()])
    configuration = run_path(str(Path(project_settings.__file__)))
    assert configuration["MAILERS"]["default"]["BACKEND"] == "pyracast.mail.gmail.EmailBackend"
    with override_settings(
        MAILERS=configuration["MAILERS"], DEFAULT_FROM_EMAIL=SENDER, DEBUG=False
    ):
        assert check_authentication_mail_delivery() == []
        backend = mail.mailers["default"]
    assert isinstance(backend, EmailBackend)


@pytest.mark.parametrize("missing", ["client_id", "client_secret", "refresh_token"])
def test_gmail_deployment_check_rejects_incomplete_oauth(missing: str) -> None:
    options = {**OPTIONS, missing: ""}
    with override_settings(
        MAILERS={"default": {"BACKEND": "pyracast.mail.gmail.EmailBackend", "OPTIONS": options}},
        DEFAULT_FROM_EMAIL=SENDER,
        DEBUG=False,
    ):
        assert [error.id for error in check_authentication_mail_delivery()] == ["accounts.E001"]


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_allauth_signup_sends_verification_using_gmail(
    gmail_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return (
            token()
            if str(request.url) == TOKEN_URL
            else httpx.Response(200, json={"id": "verified"})
        )

    gmail_transport(respond)
    with override_settings(MAILERS=MAILERS, DEFAULT_FROM_EMAIL=SENDER):
        response = signup(Client(enforce_csrf_checks=True))
    assert response.status_code == 401
    assert any(flow["id"] == "verify_email" for flow in response.json()["data"]["flows"])
    assert len(requests) == 2
    raw = base64.urlsafe_b64decode(json.loads(requests[1].content)["raw"])
    mime = BytesParser(policy=policy.default).parsebytes(raw)
    assert str(mime["To"]).lower() == "owner@example.com"
    assert mime["Subject"] == "Verify your email"
    assert mime["X-Pyracast-Mail"] == "account/email/email_confirmation_signup"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("client_id", ""),
        ("client_secret", ""),
        ("refresh_token", ""),
        ("timeout", 0),
    ],
)
def test_gmail_rejects_invalid_transport_configuration(key: str, value: object) -> None:
    options: dict[str, Any] = {**OPTIONS, key: value}
    with pytest.raises(ImproperlyConfigured, match="Gmail"):
        EmailBackend(**options)


@override_settings(DEFAULT_FROM_EMAIL="invalid")
def test_gmail_rejects_an_invalid_sender() -> None:
    options: dict[str, Any] = dict(OPTIONS)
    with pytest.raises(ImproperlyConfigured, match="Gmail"):
        EmailBackend(**options)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"access_token": 1},
        {"access_token": ""},
        {"access_token": "private", "token_type": 1},
        {"access_token": "private", "token_type": "Basic"},
        {"access_token": "private", "token_type": "Bearer", "scope": 1},
    ],
)
def test_malformed_oauth_response_is_a_safe_non_send(
    gmail_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    payload: object,
) -> None:
    gmail_transport(lambda _request: httpx.Response(200, json=payload))
    with override_settings(MAILERS=MAILERS), pytest.raises(NotSentError, match="OAuth"):
        send(message())


def test_auth_failure_honors_fail_silently_without_sending(
    gmail_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
) -> None:
    gmail_transport(lambda _request: httpx.Response(400, json={"error": "invalid_grant"}))
    options: dict[str, Any] = {**OPTIONS, "fail_silently": True}
    backend = EmailBackend(**options)
    assert backend.send_messages([message()]) == 0


@pytest.mark.parametrize("mode", ["wrong_sender", "invalid_header", "no_recipient"])
def test_invalid_message_cannot_reach_gmail_send(
    gmail_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    mode: str,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return token()

    gmail_transport(respond)
    email = message()
    if mode == "wrong_sender":
        email.from_email = "other@pyracast.app"
    elif mode == "invalid_header":
        email.subject = "Invalid\r\nInjected: header"
    else:
        email.to = []
    options: dict[str, Any] = dict(OPTIONS)
    backend = EmailBackend(**options)
    if mode == "no_recipient":
        assert backend.send_messages([email]) == 0
    else:
        with pytest.raises(PermanentlyNotSentError, match="invalid"):
            backend.send_messages([email])
    assert len(requests) == 1


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"error": {"errors": []}},
        {"error": {"errors": [{"reason": "forbidden"}]}},
        {"error": {"errors": [{"reason": ["invalid"]}]}},
    ],
)
def test_nonquota_permission_rejection_cannot_be_retried(
    gmail_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    payload: object,
) -> None:
    gmail_transport(
        lambda request: (
            token() if str(request.url) == TOKEN_URL else httpx.Response(403, json=payload)
        )
    )
    with override_settings(MAILERS=MAILERS), pytest.raises(PermanentlyNotSentError) as caught:
        send(message())
    assert caught.value.code == "mail_sender_forbidden"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not JSON"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"id": 1}),
        httpx.Response(200, json={"id": ""}),
        httpx.Response(408),
        httpx.Response(302),
    ],
)
def test_unverifiable_acceptance_is_uncertain(
    gmail_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    response: httpx.Response,
) -> None:
    gmail_transport(lambda request: token() if str(request.url) == TOKEN_URL else response)
    with override_settings(MAILERS=MAILERS), pytest.raises(DeliveryUncertainError):
        send(message())


@pytest.mark.parametrize("authentication_rejected", [False, True])
def test_gmail_cleanup_cannot_change_known_acceptance_or_auth_rejection(
    gmail_transport: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    monkeypatch: pytest.MonkeyPatch,
    authentication_rejected: bool,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(401) if authentication_rejected else token()
        return httpx.Response(200, json={"id": "accepted"})

    def close(_transport: httpx.MockTransport) -> None:
        raise httpx.ReadError("private cleanup error")

    gmail_transport(respond)
    monkeypatch.setattr(httpx.MockTransport, "close", close)
    with override_settings(MAILERS=MAILERS):
        if authentication_rejected:
            with pytest.raises(NotSentError, match="OAuth"):
                send(message())
        else:
            send(message())
