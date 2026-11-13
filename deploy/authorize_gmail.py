import argparse
import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_EXCHANGE_ENDPOINT = "https://oauth2.googleapis.com/token"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_KEYS = {
    "PYRACAST_GMAIL_CLIENT_ID",
    "PYRACAST_GMAIL_CLIENT_SECRET",
    "PYRACAST_GMAIL_REFRESH_TOKEN",
}


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str


def credential_string(value: object) -> str:
    if not isinstance(value, str) or not value or any(item in value for item in "\r\n\x00"):
        raise ValueError("Invalid OAuth credentials.")
    return value


def load_client(path: Path) -> OAuthClient:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("installed"), dict):
        raise ValueError("Use a Google Desktop app client JSON file.")
    client = payload["installed"]
    if (
        client.get("auth_uri")
        not in {
            AUTHORIZATION_URL,
            "https://accounts.google.com/o/oauth2/auth",
        }
        or client.get("token_uri") != OAUTH_EXCHANGE_ENDPOINT
    ):
        raise ValueError("OAuth client endpoints must belong to Google.")
    return OAuthClient(
        credential_string(client.get("client_id")), credential_string(client.get("client_secret"))
    )


def load_runtime(path: Path, *, replace: bool) -> dict[str, str]:
    if path.is_symlink():
        raise ValueError("Runtime secrets must be a regular file.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or not payload
        or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in payload.items()
        )
    ):
        raise ValueError("Runtime secrets must be an existing JSON object of strings.")
    if not replace and GMAIL_KEYS.intersection(payload):
        raise ValueError("Gmail credentials already exist; use --replace to authorize again.")
    return payload


def merge_runtime(
    path: Path, client: OAuthClient, refresh_token: str, sender: str, *, replace: bool
) -> None:
    values = load_runtime(path, replace=replace)
    values.update(
        {
            "PYRACAST_EMAIL_BACKEND": "gmail",
            "PYRACAST_FROM_EMAIL": sender,
            "PYRACAST_GMAIL_CLIENT_ID": client.client_id,
            "PYRACAST_GMAIL_CLIENT_SECRET": client.client_secret,
            "PYRACAST_GMAIL_REFRESH_TOKEN": credential_string(refresh_token),
        }
    )
    descriptor, temporary = tempfile.mkstemp(prefix=".gmail-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(values, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def exchange_code(client: OAuthClient, code: str, verifier: str, redirect_uri: str) -> str:
    request = urllib.request.Request(
        OAUTH_EXCHANGE_ENDPOINT,
        data=urlencode(
            {
                "client_id": client.client_id,
                "client_secret": client.client_secret,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }
        ).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 -- Fixed Google HTTPS endpoint.
        payload = json.load(response)
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("scope"), str)
        or payload["scope"].split() != [GMAIL_SCOPE]
    ):
        raise ValueError("Gmail authorization must grant only gmail.send.")
    credential_string(payload.get("access_token"))
    if payload.get("token_type") != "Bearer":
        raise ValueError("Invalid OAuth token response.")
    if not payload.get("refresh_token"):
        raise ValueError(
            "Google did not issue an offline refresh token; authorize with consent again."
        )
    return credential_string(payload["refresh_token"])


def authorize(
    client: OAuthClient,
    sender: str,
    *,
    timeout_seconds: float = 180,
    open_browser: Callable[[str], object] = webbrowser.open,
) -> str:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    code: str | None = None
    denied = False
    expected_host = ""

    class Callback(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(min(timeout_seconds, 5))

        def log_message(self, *_args: object, **_kwargs: object) -> None:
            pass

        # BaseHTTPRequestHandler dispatches GET requests using this exact method name.
        # noinspection PyPep8Naming
        def do_GET(self) -> None:
            nonlocal code, denied
            try:
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query, strict_parsing=True)
            except ValueError:
                parsed = urlsplit("/")
                query = {}
            valid = (
                parsed.path == "/"
                and self.headers.get("Host") == expected_host
                and query.get("state") == [state]
                and (len(query.get("code", [])) == 1 or len(query.get("error", [])) == 1)
                and not ("code" in query and "error" in query)
            )
            if valid:
                code = query.get("code", [None])[0]
                denied = "error" in query
            message = (
                b"Gmail authorization response received. You can close this window."
                if valid
                else b"Invalid authorization response. Return to the original consent window."
            )
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)

    deadline = time.monotonic() + timeout_seconds
    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = min(timeout_seconds, 1)
        expected_host = f"127.0.0.1:{server.server_port}"
        # OAuth loopback uses HTTP only on localhost; state and PKCE bind the response.
        redirect_uri = f"http://{expected_host}/"
        url = (
            AUTHORIZATION_URL
            + "?"
            + urlencode(
                {
                    "client_id": client.client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": GMAIL_SCOPE,
                    "access_type": "offline",
                    "prompt": "consent",
                    "login_hint": sender,
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )
        open_browser(url)
        while code is None and not denied and time.monotonic() < deadline:
            server.handle_request()
    if denied:
        raise ValueError("Gmail authorization was denied.")
    if code is None:
        raise TimeoutError("Gmail authorization timed out.")
    return exchange_code(client, code, verifier, redirect_uri)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Authorize one Gmail sender without reading its mailbox."
    )
    parser.add_argument("--client", type=Path, default=Path("secrets/gmail-client.json"))
    parser.add_argument("--runtime", type=Path, default=Path("secrets/runtime.json"))
    parser.add_argument("--sender", default="info@pyracast.app")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    arguments = parser.parse_args()
    try:
        sender = credential_string(arguments.sender)
        if (
            sender.count("@") != 1
            or not all(sender.split("@"))
            or any(character.isspace() for character in sender)
        ):
            raise ValueError("Use the sender's primary mailbox address.")
        client = load_client(arguments.client)
        load_runtime(arguments.runtime, replace=arguments.replace)
        print("Approve Gmail send access using the configured sender account.")
        refresh_token = authorize(
            client, sender, open_browser=print if arguments.no_browser else webbrowser.open
        )
        merge_runtime(arguments.runtime, client, refresh_token, sender, replace=arguments.replace)
    except OSError, ValueError, urllib.error.URLError:
        raise SystemExit(
            "Gmail authorization failed. Check the Desktop client, consent, sender "
            "and local secret files."
        ) from None
    print("Gmail credentials saved locally. Upload the updated runtime secret to activate them.")


if __name__ == "__main__":
    main()
