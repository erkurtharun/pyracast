from typing import Any

import pytest
from allauth.socialaccount.models import SocialApp
from django.db import connection
from django.http import HttpRequest
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from pyracast.accounts.social import SocialAccountAdapter

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("provider", "client_id", "expected"),
    [
        (None, None, ["google", "microsoft"]),
        ("google", None, ["google"]),
        (None, "microsoft-client", ["microsoft"]),
        ("google", "google-client", ["google"]),
        ("google", "missing-client", []),
        ("unknown", None, []),
    ],
)
def test_deployment_apps_are_selected_without_database_credentials(
    provider: str | None, client_id: str | None, expected: list[str]
) -> None:
    providers = {
        name: {"APP": {"client_id": f"{name}-client", "secret": "unused-test-credential"}}
        for name in ("google", "microsoft")
    }
    SocialApp.objects.create(provider="google", **providers["google"]["APP"])
    adapter = SocialAccountAdapter()
    request = HttpRequest()
    with (
        override_settings(SOCIALACCOUNT_PROVIDERS=providers),
        CaptureQueriesContext(connection) as queries,
    ):
        apps = adapter.list_apps(request, provider, client_id)
        app = adapter.get_app(request, "google", "google-client")
    assert [item.provider for item in apps] == expected
    assert all(item.pk is None for item in [*apps, app])
    assert app.client_id == "google-client"
    assert len(queries) == 0


@pytest.mark.parametrize(
    "configuration",
    [
        {},
        {"APP": None},
        {"APP": {"client_id": "", "secret": "unused-test-credential"}},
        {"APP": {"client_id": "google-client", "secret": ""}},
    ],
)
def test_incomplete_deployment_credentials_cannot_enable_a_provider(
    configuration: dict[str, Any],
) -> None:
    with override_settings(SOCIALACCOUNT_PROVIDERS={"google": configuration}):
        assert SocialAccountAdapter().list_apps(None) == []
