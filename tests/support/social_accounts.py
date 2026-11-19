import json
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from django.conf import settings
from django.http import HttpResponse, HttpResponseBase
from django.test import Client

AUTH = "/api/auth/browser/v1/"


CALLBACK = "https://testserver/app/?social_callback=1"


PROVIDERS = {
    provider: {
        "APP": {"client_id": f"{provider}-client", "secret": "test-client-secret"},
        "SCOPE": ["openid", "email"] if provider == "google" else ["User.Read"],
        "OAUTH_PKCE_ENABLED": True,
    }
    for provider in ("google", "microsoft")
}


class ProviderRedirectParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


def provider_destination(response: HttpResponseBase) -> str:
    assert isinstance(response, HttpResponse)
    assert response.status_code == 200
    assert response["Content-Type"] == "text/html; charset=utf-8"
    assert "no-store" in response["Cache-Control"].split(", ")
    assert response["Referrer-Policy"] == "no-referrer"
    assert not response.has_header("Location")
    parser = ProviderRedirectParser()
    parser.feed(response.content.decode())
    refresh = [
        attrs["content"]
        for tag, attrs in parser.elements
        if tag == "meta" and attrs.get("http-equiv") == "refresh"
    ]
    assert len(refresh) == 1
    assert refresh[0] is not None
    assert refresh[0].startswith("0; url=")
    destination = refresh[0].removeprefix("0; url=")
    assert [attrs["href"] for tag, attrs in parser.elements if tag == "a"] == [destination]
    return destination


def begin(client: Client, provider: str = "google", process: str = "login") -> str:
    token = client.get("/api/session/").json()["csrf_token"]
    response = client.post(
        AUTH + "auth/provider/redirect",
        {"provider": provider, "process": process, "callback_url": CALLBACK},
        HTTP_X_CSRFTOKEN=token,
    )
    destination = provider_destination(response)
    query = parse_qs(urlsplit(destination).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]
    scheme = settings.ACCOUNT_DEFAULT_HTTP_PROTOCOL
    assert query["redirect_uri"] == [
        f"{scheme}://testserver/api/accounts/{provider}/login/callback/"
    ]
    assert "offline_access" not in query["scope"][0]
    assert set(query["scope"][0].split()) == (
        {"openid", "email"} if provider == "google" else {"User.Read"}
    )
    if provider == "microsoft":
        assert urlsplit(destination).path == "/common/oauth2/v2.0/authorize"
    return query["state"][0]


def complete(client: Client, state: str, provider: str = "google") -> Any:
    return client.get(
        f"/api/accounts/{provider}/login/callback/", {"state": state, "code": "provider-code"}
    )


def serve_provider_profile(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "sub": "google-identity",
        "email": "solar@gmail.com",
        "email_verified": True,
        "id": "microsoft-identity",
        "mail": "solar@example.com",
        "jobTitle": "Private profile detail",
    }

    def request(
        self: requests.Session, method: str, url: str, *args: Any, **kwargs: Any
    ) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response.headers["content-type"] = "application/json"
        identity_keys = (
            {"id", "mail", "userPrincipalName", "jobTitle"}
            if "graph.microsoft.com" in url
            else {"sub", "email", "email_verified", "hd", "jobTitle"}
        )
        before_profile = profile.get("_before_profile")
        if not url.endswith("/token") and before_profile is not None:
            before_profile()
        response._content = json.dumps(
            {"access_token": "test-access-token", "token_type": "Bearer", "expires_in": 3600}
            if url.endswith("/token")
            else {key: value for key, value in profile.items() if key in identity_keys}
        ).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    return profile
