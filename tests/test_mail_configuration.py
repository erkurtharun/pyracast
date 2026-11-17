from collections.abc import Iterator
from io import StringIO
from ipaddress import IPv4Address
from pathlib import Path
from runpy import run_path
from typing import Any

import httpx
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from pyracast import settings as project_settings
from pyracast.accounts.checks import check_authentication_mail_delivery
from pyracast.mail.gmail import TOKEN_URL
from tests.smtp_server import Script, loopback_smtp

PUBLIC_SENDER = "Pyracast <info@pyracast.app>"
SMTP_BACKEND = "pyracast.mail.smtp.EmailBackend"
GMAIL_BACKEND = "pyracast.mail.gmail.EmailBackend"
GMAIL_OPTIONS = {"client_id": "client", "client_secret": "secret", "refresh_token": "refresh"}


def configuration(monkeypatch: pytest.MonkeyPatch, **environment: str | None) -> dict[str, Any]:
    monkeypatch.setattr("pyracast.environment.load_environment", lambda: None)
    monkeypatch.setenv("PYRACAST_FROM_EMAIL", PUBLIC_SENDER)
    monkeypatch.setenv("PYRACAST_EMAIL_HOST", "smtp.example.com")
    for key, value in environment.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return run_path(str(Path(project_settings.__file__)))


def deploy_errors(mailers: dict[str, Any], sender: str = PUBLIC_SENDER) -> list[str | None]:
    with override_settings(MAILERS=mailers, DEFAULT_FROM_EMAIL=sender, DEBUG=False):
        return [error.id for error in check_authentication_mail_delivery()]


def test_public_smtp_relay_with_starttls_passes_the_deploy_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailers = configuration(monkeypatch, PYRACAST_EMAIL_PORT=None)["MAILERS"]

    assert mailers["default"]["OPTIONS"]["port"] == 587
    assert deploy_errors(mailers) == []


def test_configuration_does_not_reload_local_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def load_local_environment() -> None:
        monkeypatch.setenv("PYRACAST_EMAIL_HOST", "developer-relay.example.com")

    monkeypatch.setattr("pyracast.environment.load_environment", load_local_environment)

    mailers = configuration(monkeypatch, PYRACAST_EMAIL_HOST=None)["MAILERS"]

    assert mailers["default"]["OPTIONS"]["host"] == ""


def test_unset_smtp_host_fails_the_deploy_check(monkeypatch: pytest.MonkeyPatch) -> None:
    mailers = configuration(monkeypatch, PYRACAST_EMAIL_HOST=None)["MAILERS"]

    assert deploy_errors(mailers) == ["accounts.E001"]


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "127.0.0.1",
        "::1",
        "127.0.0.53",
        str(IPv4Address(0)),
        "::",
        "[::1]",
        "[::]",
        "127.1",
    ],
)
def test_loopback_smtp_host_fails_the_deploy_check(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    mailers = configuration(monkeypatch, PYRACAST_EMAIL_HOST=host)["MAILERS"]

    assert deploy_errors(mailers) == ["accounts.E001"]


def test_smtp_without_transport_security_fails_the_deploy_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailers = configuration(monkeypatch, PYRACAST_EMAIL_TLS="0", PYRACAST_EMAIL_SSL="0")["MAILERS"]

    assert deploy_errors(mailers) == ["accounts.E001"]


def test_smtp_with_both_transport_modes_fails_the_deploy_check() -> None:
    options = {"host": "smtp.example.com", "use_tls": True, "use_ssl": True}

    assert deploy_errors({"default": {"BACKEND": SMTP_BACKEND, "OPTIONS": options}}) == [
        "accounts.E001"
    ]


def test_implicit_tls_needs_only_the_ssl_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    mailers = configuration(
        monkeypatch,
        PYRACAST_EMAIL_SSL="1",
        PYRACAST_EMAIL_TLS=None,
        PYRACAST_EMAIL_PORT=None,
    )["MAILERS"]

    options = mailers["default"]["OPTIONS"]
    assert (options["use_ssl"], options["use_tls"]) == (True, False)
    assert options["port"] == 465
    assert deploy_errors(mailers) == []


@pytest.mark.parametrize("ssl", ["0", "1"])
def test_explicit_smtp_port_overrides_transport_default(
    monkeypatch: pytest.MonkeyPatch, ssl: str
) -> None:
    mailers = configuration(
        monkeypatch,
        PYRACAST_EMAIL_SSL=ssl,
        PYRACAST_EMAIL_TLS=None,
        PYRACAST_EMAIL_PORT="2525",
    )["MAILERS"]

    assert mailers["default"]["OPTIONS"]["port"] == 2525
    assert deploy_errors(mailers) == []


def test_invalid_smtp_port_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ImproperlyConfigured, match="PYRACAST_EMAIL_PORT"):
        configuration(monkeypatch, PYRACAST_EMAIL_PORT="smtp")


@pytest.mark.parametrize("port", ["0", "65536", "70000"])
def test_smtp_port_outside_the_tcp_range_fails_the_deploy_check(
    monkeypatch: pytest.MonkeyPatch, port: str
) -> None:
    mailers = configuration(monkeypatch, PYRACAST_EMAIL_PORT=port)["MAILERS"]

    assert deploy_errors(mailers) == ["accounts.E001"]


@pytest.mark.parametrize("sender", ["Pyracast <ops@invalid>", "Pyracast <ops@pyracast>"])
def test_smtp_sender_without_a_valid_mailbox_fails_the_deploy_check(
    monkeypatch: pytest.MonkeyPatch, sender: str
) -> None:
    mailers = configuration(monkeypatch)["MAILERS"]

    assert deploy_errors(mailers, sender=sender) == ["accounts.E001"]


def test_gmail_options_carry_no_separate_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    mailers = configuration(
        monkeypatch,
        PYRACAST_EMAIL_BACKEND="gmail",
        **{f"PYRACAST_GMAIL_{key.upper()}": value for key, value in GMAIL_OPTIONS.items()},
    )["MAILERS"]

    assert set(mailers["default"]["OPTIONS"]) == {
        "client_id",
        "client_secret",
        "refresh_token",
        "timeout",
    }
    assert deploy_errors(mailers) == []


def test_gmail_sender_the_backend_refuses_fails_the_deploy_check() -> None:
    mailers = {"default": {"BACKEND": GMAIL_BACKEND, "OPTIONS": GMAIL_OPTIONS}}

    assert deploy_errors(mailers, sender="Pyracast <ops@pyracast>") == ["accounts.E001"]


def test_gmail_timeout_the_backend_refuses_fails_the_deploy_check() -> None:
    options = {**GMAIL_OPTIONS, "timeout": 0}

    assert deploy_errors({"default": {"BACKEND": GMAIL_BACKEND, "OPTIONS": options}}) == [
        "accounts.E001"
    ]


@pytest.fixture
def relay() -> Iterator[Script]:
    script = Script(extensions=["8BITMIME", "AUTH PLAIN"])
    with loopback_smtp(script) as port:
        options = {
            "host": "127.0.0.1",
            "port": port,
            "use_tls": False,
            "timeout": 5,
            "username": "relay-user",
            "password": "relay-secret",
        }
        with override_settings(
            DEFAULT_FROM_EMAIL=PUBLIC_SENDER,
            MAILERS={"default": {"BACKEND": SMTP_BACKEND, "OPTIONS": options}},
        ):
            yield script


def test_probe_authenticates_without_sending(relay: Script) -> None:
    output = StringIO()

    call_command("check_mail", stdout=output)

    assert relay.transactions == []
    assert "AUTH" in relay.commands
    assert "MAIL" not in relay.commands
    assert relay.commands[-1] == "QUIT"
    assert "accepted the connection" in output.getvalue()


@pytest.mark.parametrize(
    ("change", "arguments", "exit_code", "failure"),
    [
        ({"auth_reply": "535 5.7.8 rejected"}, [], 3, "mail_credentials_rejected"),
        ({"auth_reply": "454 4.7.0 later"}, [], 8, "mail_not_sent"),
        ({"drop_at": "connect"}, [], 8, "mail_not_sent"),
        ({"mail_reply": "550 5.7.1 refused"}, ["--send-to", "op@example.com"], 6, "mail_rejected"),
        ({"drop_at": "data_end"}, ["--send-to", "op@example.com"], 9, "mail_outcome_uncertain"),
    ],
)
def test_failed_check_exits_with_the_code_of_its_failure(
    relay: Script, change: dict[str, str], arguments: list[str], exit_code: int, failure: str
) -> None:
    for name, value in change.items():
        setattr(relay, name, value)

    with pytest.raises(CommandError) as caught:
        call_command("check_mail", *arguments, stdout=StringIO())

    assert caught.value.returncode == exit_code
    assert failure in str(caught.value)
    assert "relay-secret" not in str(caught.value)


def test_invalid_configuration_exits_with_its_own_code() -> None:
    options = {"host": "127.0.0.1", "use_tls": True, "use_ssl": True}
    with (
        override_settings(MAILERS={"default": {"BACKEND": SMTP_BACKEND, "OPTIONS": options}}),
        pytest.raises(CommandError) as caught,
    ):
        call_command("check_mail", stdout=StringIO())

    assert caught.value.returncode == 2
    assert "mail_configuration_invalid" in str(caught.value)


def test_revoked_gmail_authorization_exits_with_its_own_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TOKEN_URL
        return httpx.Response(400, json={"error": "invalid_grant"})

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    with (
        override_settings(
            DEFAULT_FROM_EMAIL=PUBLIC_SENDER,
            MAILERS={"default": {"BACKEND": GMAIL_BACKEND, "OPTIONS": GMAIL_OPTIONS}},
        ),
        pytest.raises(CommandError) as caught,
    ):
        call_command("check_mail", stdout=StringIO())

    assert caught.value.returncode == 4
    assert "mail_authorization_revoked" in str(caught.value)


def test_test_mail_needs_a_valid_address() -> None:
    with pytest.raises(CommandError, match="--send-to") as caught:
        call_command("check_mail", "--send-to", "not an address", stdout=StringIO())

    assert caught.value.returncode == 1
