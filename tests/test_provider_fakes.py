from collections.abc import Callable
from typing import Any

import pytest
import requests

from tests.support.account_mail import provider_requests
from tests.support.social_accounts import serve_provider_profile

FAKES: dict[str, Callable[[pytest.MonkeyPatch], dict[str, Any]]] = {
    "account_mail": provider_requests,
    "social_accounts": serve_provider_profile,
}
TOKEN = {"access_token": "test-access-token", "token_type": "Bearer", "expires_in": 3600}
IDENTITY = {"sub": "google-identity", "id": "microsoft-identity"}


@pytest.mark.parametrize("fake", FAKES)
def test_the_sign_in_fakes_answer_the_token_and_profile_endpoints_of_each_provider(
    monkeypatch: pytest.MonkeyPatch, fake: str
) -> None:
    FAKES[fake](monkeypatch).update(IDENTITY)
    session = requests.Session()

    for token_url in (
        "https://oauth2.googleapis.com/token",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
    ):
        assert session.post(token_url).json() == TOKEN
    google = session.get("https://www.googleapis.com/oauth2/v2/userinfo").json()
    microsoft = session.get("https://graph.microsoft.com/v1.0/me").json()

    assert (google["sub"], "id" in google) == ("google-identity", False)
    assert (microsoft["id"], "sub" in microsoft) == ("microsoft-identity", False)


@pytest.mark.parametrize("fake", FAKES)
@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "https://evil.example/graph.microsoft.com"),
        ("GET", "https://evil.example/?u=graph.microsoft.com"),
        ("GET", "https://graph.microsoft.com.evil.example/v1.0/me"),
        ("GET", "http://graph.microsoft.com/v1.0/me"),
        ("GET", "https://graph.microsoft.com/v1.0/users"),
        ("GET", "https://www.googleapis.com/oauth2/v1/certs"),
        ("POST", "https://evil.example/token"),
        ("POST", "https://oauth2.googleapis.com.evil.example/token"),
        ("POST", "https://login.microsoftonline.com/other-tenant/oauth2/v2.0/token"),
    ],
)
def test_the_sign_in_fakes_fail_on_any_other_url(
    monkeypatch: pytest.MonkeyPatch, fake: str, method: str, url: str
) -> None:
    FAKES[fake](monkeypatch)

    with pytest.raises(AssertionError, match="Unexpected provider URL"):
        requests.Session().request(method, url)
