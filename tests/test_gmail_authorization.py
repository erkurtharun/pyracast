import base64
import hashlib
import http.client
import json
import sys
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.message import Message
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

import pytest

from deploy.authorize_gmail import (
    AUTHORIZATION_URL,
    GMAIL_SCOPE,
    GMAIL_SEND_ENDPOINT,
    OAUTH_EXCHANGE_ENDPOINT,
    Grant,
    OAuthClient,
    authorize,
    exchange_code,
    load_client,
    main,
    merge_runtime,
)


def token_response(**overrides: object) -> bytes:
    return json.dumps(
        {
            "scope": GMAIL_SCOPE,
            "access_token": "private-access",
            "refresh_token": "private-refresh",
            "token_type": "Bearer",
            **overrides,
        }
    ).encode()


def client_file(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": OAUTH_EXCHANGE_ENDPOINT,
                    "client_id": "operator-client.apps.googleusercontent.com",
                    "client_secret": "private-client",
                }
            }
        )
    )
    return path


@pytest.mark.parametrize(
    "payload",
    [{}, {"web": {}}, {"installed": {}}, {"installed": {"token_uri": "https://evil.test"}}],
)
def test_oauth_requires_google_desktop_client(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "client.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=r"Desktop|Google"):
        load_client(path)


def test_oauth_merges_only_mail_keys_and_requires_explicit_replacement(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    original = {
        "PYRACAST_SECRET_KEY": "keep-encryption-key",
        "PGPASSWORD": "keep-database-password",
        "RABBITMQ_DEFAULT_PASS": "keep-broker-password",
        "PYRACAST_EMAIL_BACKEND": "smtp",
        "PYRACAST_EMAIL_PASSWORD": "keep-existing-smtp-password",
    }
    runtime.write_text(json.dumps(original))
    client = load_client(client_file(tmp_path / "client.json"))
    merge_runtime(runtime, client, "private-refresh", "info@pyracast.app", replace=False)
    saved = json.loads(runtime.read_text())
    assert saved == {
        **original,
        "PYRACAST_EMAIL_BACKEND": "gmail",
        "PYRACAST_FROM_EMAIL": "info@pyracast.app",
        "PYRACAST_GMAIL_CLIENT_ID": client.client_id,
        "PYRACAST_GMAIL_CLIENT_SECRET": "private-client",
        "PYRACAST_GMAIL_REFRESH_TOKEN": "private-refresh",
    }
    before = runtime.read_bytes()
    with pytest.raises(ValueError, match="--replace"):
        merge_runtime(runtime, client, "another-refresh", "info@pyracast.app", replace=False)
    assert runtime.read_bytes() == before
    merge_runtime(runtime, client, "new-refresh", "info@pyracast.app", replace=True)
    assert json.loads(runtime.read_text()).get("PYRACAST_GMAIL_REFRESH_TOKEN") == "new-refresh"
    assert not list(tmp_path.glob(".gmail-*"))


def test_oauth_failed_atomic_replace_preserves_existing_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text('{"PYRACAST_SECRET_KEY":"preserved"}')
    before = runtime.read_bytes()

    def fail(source: str, target: Path) -> None:
        raise OSError("Disk failure")

    monkeypatch.setattr("deploy.authorize_gmail.os.replace", fail)
    with pytest.raises(OSError, match="Disk failure"):
        merge_runtime(
            runtime,
            OAuthClient("client", "private-client"),
            "private-refresh",
            "info@pyracast.app",
            replace=False,
        )
    assert runtime.read_bytes() == before
    assert not list(tmp_path.glob(".gmail-*"))


@pytest.mark.parametrize(
    "payload",
    [
        {"scope": "https://www.googleapis.com/auth/gmail.readonly"},
        {"scope": GMAIL_SCOPE + " openid"},
        {"scope": None},
        {"scope": ""},
        {"refresh_token": None},
        {"refresh_token": "private\nrefresh"},
        {"token_type": "invalid"},
        {"access_token": ""},
    ],
)
def test_oauth_rejects_wrong_scope_or_missing_credentials_without_disclosure(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    def respond(request: Request, *, timeout: int) -> BytesIO:
        return BytesIO(token_response(**payload))

    monkeypatch.setattr("deploy.authorize_gmail.urllib.request.urlopen", respond)
    with pytest.raises(ValueError, match=r"Gmail|OAuth|Google") as error:
        exchange_code(
            OAuthClient("client", "private-client"), "private-code", "verifier", "loopback"
        )
    assert "private" not in str(error.value)
    assert capsys.readouterr() == ("", "")


def test_oauth_loopback_rejects_forged_state_and_exchanges_pkce_offline_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parameters: dict[str, list[str]] = {}
    requests: list[Request] = []
    browser_responses: list[tuple[int, bytes]] = []

    def respond(request: Request, *, timeout: int) -> BytesIO:
        requests.append(request)
        return BytesIO(token_response())

    def callback(url: str) -> None:
        parsed = urlsplit(url)
        assert parsed.scheme == "http"
        assert parsed.hostname == "127.0.0.1"
        connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=5)
        try:
            connection.request("GET", parsed.path + "?" + parsed.query)
            response = connection.getresponse()
            assert response.getheader("Cache-Control") == "no-store"
            browser_responses.append((response.status, response.read()))
        finally:
            connection.close()

    def complete(url: str) -> None:
        parsed = urlsplit(url)
        assert parsed.scheme + "://" + parsed.netloc + parsed.path == AUTHORIZATION_URL
        parameters.update(parse_qs(parsed.query))
        redirect = parameters["redirect_uri"][0]
        callback(redirect + "?" + urlencode({"state": "forged-state", "code": "private-code"}))
        callback(
            redirect + "?" + urlencode({"state": parameters["state"][0], "code": "private-code"})
        )

    monkeypatch.setattr("deploy.authorize_gmail.urllib.request.urlopen", respond)
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert authorize(
            OAuthClient("client", "private-client"),
            "info@pyracast.app",
            timeout_seconds=5,
            open_browser=lambda url: executor.submit(complete, url),
        ) == Grant("private-access", "private-refresh")
    assert parameters["scope"] == [GMAIL_SCOPE]
    assert parameters["access_type"] == ["offline"]
    assert parameters["prompt"] == ["consent"]
    assert parameters["login_hint"] == ["info@pyracast.app"]
    assert parameters["code_challenge_method"] == ["S256"]
    (request,) = requests
    assert request.full_url == OAUTH_EXCHANGE_ENDPOINT
    assert isinstance(request.data, bytes)
    body = parse_qs(request.data.decode())
    verifier = body["code_verifier"][0]
    assert 43 <= len(verifier) <= 128
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    assert parameters["code_challenge"] == [expected_challenge]
    assert body["redirect_uri"] == parameters["redirect_uri"]
    assert body["grant_type"] == ["authorization_code"]
    assert [status for status, _ in browser_responses] == [400, 200]
    assert all(b"private" not in content for _, content in browser_responses)
    assert capsys.readouterr() == ("", "")


def test_oauth_callback_timeout_never_exchanges_a_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_exchange(*args: object) -> str:
        raise AssertionError("No callback must not exchange credentials.")

    monkeypatch.setattr("deploy.authorize_gmail.exchange_code", forbidden_exchange)
    with pytest.raises(TimeoutError):
        authorize(
            OAuthClient("client", "private-client"),
            "info@pyracast.app",
            timeout_seconds=0.01,
            open_browser=lambda url: None,
        )


def test_oauth_main_redacts_failures_and_preserves_existing_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text('{"PYRACAST_SECRET_KEY":"preserved"}')
    before = runtime.read_bytes()
    client = client_file(tmp_path / "client.json")

    def fail(*args: object, **kwargs: object) -> str:
        raise ValueError("private-refresh private-client private-code")

    monkeypatch.setattr("deploy.authorize_gmail.authorize", fail)
    monkeypatch.setattr(
        sys, "argv", ["authorize_gmail", "--client", str(client), "--runtime", str(runtime)]
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert "private" not in str(error.value)
    assert "private" not in str(capsys.readouterr())
    assert runtime.read_bytes() == before


@pytest.mark.parametrize("accepted", [True, False])
def test_credentials_are_saved_only_after_the_sender_mails_itself(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    accepted: bool,
) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text('{"PYRACAST_SECRET_KEY":"preserved"}')
    before = runtime.read_bytes()
    client = client_file(tmp_path / "client.json")
    requests: list[Request] = []
    refusal = urllib.error.HTTPError(
        "https://gmail.googleapis.com", 403, "Forbidden", Message(), BytesIO(b"private")
    )

    def respond(request: Request, *, timeout: int) -> BytesIO:
        requests.append(request)
        if not accepted:
            raise refusal
        return BytesIO(b'{"id": "sent"}')

    grant = Grant("private-access", "private-refresh")
    monkeypatch.setattr("deploy.authorize_gmail.authorize", lambda *_args, **_kwargs: grant)
    monkeypatch.setattr("deploy.authorize_gmail.urllib.request.urlopen", respond)
    monkeypatch.setattr(
        sys,
        "argv",
        ["authorize_gmail", "--client", str(client), "--runtime", str(runtime), "--no-browser"],
    )

    if accepted:
        main()
        saved = json.loads(runtime.read_text())
        assert saved["PYRACAST_GMAIL_REFRESH_TOKEN"] == grant.refresh_token
    else:
        with pytest.raises(SystemExit):
            main()
        assert runtime.read_bytes() == before
    (request,) = requests
    assert request.full_url == GMAIL_SEND_ENDPOINT
    assert request.get_header("Authorization") == "Bearer private-access"
    assert isinstance(request.data, bytes)
    raw = base64.urlsafe_b64decode(json.loads(request.data)["raw"])
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    assert str(mail["From"]) == str(mail["To"]) == "info@pyracast.app"
    assert "private" not in str(capsys.readouterr())
    refusal.close()
