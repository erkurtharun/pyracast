import re
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal import auth as totp_auth
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import URLPattern, URLResolver, reverse

from pyracast.accounts.models import Language, UserLanguagePreference
from pyracast.admin import site
from pyracast.notifications.models import NotificationSettings
from tests.support.api import verified_admin
from tests.support.auth_lifecycle import totp_code
from tests.support.step_up import with_proof

pytestmark = pytest.mark.django_db
CONFIRMATION = "admin/workspace/confirm.html"
# Signing out, choosing a language and confirming with the authenticator are the console's only
# writes that need no recent confirmation; the login route only redirects.
UNGUARDED = {"login", "logout", "workspace_language", "workspace_confirm"}
SAMPLES = {"app_label": "regions", "url": "unknown/"}
SESSION_TABLES = ('"django_session"', '"usersessions_usersession"')


def console_routes(
    patterns: list[URLPattern | URLResolver] | None = None, prefix: str = ""
) -> Iterator[tuple[str, str]]:
    for pattern in site.get_urls() if patterns is None else patterns:
        if isinstance(pattern, URLResolver):
            yield from console_routes(pattern.url_patterns, prefix + str(pattern.pattern))
        else:
            yield prefix + str(pattern.pattern), pattern.name or "catch_all"


ROUTES = sorted(console_routes())


def concrete(route: str) -> str:
    route = re.sub(r"\(\?P<(\w+)>[^)]*\)", lambda match: SAMPLES[match[1]], route)
    route = re.sub(r"<(?:\w+:)?\w+>", "1", route)
    return "/admin/" + route.replace("^", "").replace("$", "")


def refused(response: Any) -> bool:
    return response.status_code == 403 and CONFIRMATION in {
        template.name for template in response.templates
    }


def writes(queries: CaptureQueriesContext) -> list[str]:
    return [
        query["sql"]
        for query in queries.captured_queries
        if re.match(r"\s*(INSERT|UPDATE|DELETE)\b", query["sql"], re.IGNORECASE)
        and not any(table in query["sql"] for table in SESSION_TABLES)
    ]


def test_the_routes_that_need_no_confirmation_are_console_routes() -> None:
    assert {name for _route, name in ROUTES} >= UNGUARDED


@pytest.mark.parametrize(("route", "name"), ROUTES, ids=[route for route, _ in ROUTES])
def test_a_console_write_without_recent_mfa_evidence_is_refused_and_stores_nothing(
    route: str, name: str
) -> None:
    client, _ = verified_admin(confirmed=False)
    client.get("/admin/")

    with CaptureQueriesContext(connection) as queries:
        response = client.post(concrete(route), {"action": "approve", "language": "tr"})

    if name in UNGUARDED:
        assert not refused(response)
    else:
        assert refused(response)
        assert writes(queries) == []


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_every_unsafe_method_needs_recent_mfa_evidence(method: str) -> None:
    client, _ = verified_admin(confirmed=False)

    assert refused(getattr(client, method)(reverse("admin:workspace_settings")))


def test_a_stale_console_write_changes_nothing_and_a_confirmed_one_saves() -> None:
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 5})
    client, user = verified_admin(confirmed=False)
    change = {"max_recipients": 8, "expected_max_recipients": 5}

    response = client.post(reverse("admin:workspace_settings"), change)

    assert refused(response)
    assert b"Nothing was saved." in response.content
    assert NotificationSettings.objects.get().max_recipients == 5
    assert not LogEntry.objects.exists()
    with_proof(client, user, "mfa", age=0, reauthenticated=True, **factor_of(user))
    assert client.post(reverse("admin:workspace_settings"), change).status_code == 302
    assert NotificationSettings.objects.get().max_recipients == 8


def factor_of(user: User) -> dict[str, Any]:
    authenticator = Authenticator.objects.filter(user=user).first()
    assert authenticator is not None
    return {"type": authenticator.type, "id": authenticator.pk}


BOUND = settings.ACCOUNT_REAUTHENTICATION_TIMEOUT
EVIDENCE: dict[str, tuple[str, float, bool, bool]] = {
    "sign-in only": ("mfa", 0, False, False),
    "confirmed now": ("mfa", 0, True, True),
    "confirmed within the bound": ("mfa", BOUND - 5, True, True),
    "confirmed at the bound": ("mfa", BOUND, True, False),
    "confirmed in the future": ("mfa", -60, True, False),
    "confirmed by password": ("password", 0, True, False),
}


@pytest.mark.parametrize("evidence", EVIDENCE)
def test_the_console_and_the_api_agree_on_recent_mfa_evidence(evidence: str) -> None:
    method, age, reauthenticated, recent = EVIDENCE[evidence]
    client, user = verified_admin(confirmed=False)
    extra = factor_of(user) if method == "mfa" else {}
    if evidence != "sign-in only":
        with_proof(client, user, method, age=age, reauthenticated=reauthenticated, **extra)

    api = client.get("/api/auth/browser/v1/account/authenticators/recovery-codes")
    console = client.post(reverse("admin:workspace_settings"), {})

    assert (api.status_code == 401) is not recent
    assert refused(console) is not recent


def test_evidence_of_a_removed_authenticator_is_not_recent() -> None:
    client, user = verified_admin(confirmed=False)
    codes = Authenticator.objects.create(user=user, type="recovery_codes", data={})
    with_proof(client, user, "mfa", age=0, reauthenticated=True, type=codes.type, id=codes.pk)
    codes.delete()

    api = client.get("/api/auth/browser/v1/account/authenticators/recovery-codes")
    console = client.post(reverse("admin:workspace_settings"), {})

    assert api.status_code == 401
    assert refused(console)


def confirmation_link(path: str) -> bytes:
    return f'href="{reverse("admin:workspace_confirm")}?next={quote(path, safe="")}"'.encode()


def test_console_pages_offer_the_confirmation_before_a_form_is_filled() -> None:
    client, user = verified_admin(confirmed=False)
    path = reverse("admin:workspace_settings")

    assert confirmation_link(path) in client.get(path).content
    with_proof(client, user, "mfa", age=0, reauthenticated=True, **factor_of(user))
    assert confirmation_link(path) not in client.get(path).content


@pytest.fixture
def operator(monkeypatch: pytest.MonkeyPatch) -> tuple[Client, str, float]:
    now = time.time()
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now))
    client, user = verified_admin(confirmed=False)
    Authenticator.objects.filter(user=user).delete()
    secret = generate_totp_secret()
    factor = TOTP.activate(user, secret).instance
    with_proof(client, user, "mfa", age=60, reauthenticated=False, type=factor.type, id=factor.pk)
    return client, secret, now


CODE_LIMITS = pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)


@CODE_LIMITS
def test_confirming_with_the_authenticator_allows_console_writes(
    operator: tuple[Client, str, float],
) -> None:
    client, secret, now = operator
    confirm = reverse("admin:workspace_confirm")
    settings_path = reverse("admin:workspace_settings")

    page = client.get(confirm, {"next": settings_path})
    wrong = client.post(confirm, {"code": "000000", "next": settings_path})
    still = client.post(settings_path, {})
    right = client.post(confirm, {"code": totp_code(secret, now), "next": settings_path})

    assert page.status_code == 200
    assert CONFIRMATION in {template.name for template in page.templates}
    assert wrong.status_code == 400
    assert refused(still)
    assert right.status_code == 302
    assert right["Location"] == settings_path
    assert not refused(client.post(settings_path, {}))


@CODE_LIMITS
@pytest.mark.parametrize(
    "destination", ["https://elsewhere.example/admin/", "/app/", "//elsewhere.example/admin/"]
)
def test_a_confirmation_returns_only_to_a_console_page(
    operator: tuple[Client, str, float], destination: str
) -> None:
    client, secret, now = operator

    response = client.post(
        reverse("admin:workspace_confirm"), {"code": totp_code(secret, now), "next": destination}
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("admin:index")


def test_a_refused_write_offers_to_return_to_the_page_it_came_from() -> None:
    client, _ = verified_admin(confirmed=False)
    origin = reverse("admin:workspace_settings")

    response = client.post(origin, {}, HTTP_REFERER=f"http://testserver{origin}")

    assert refused(response)
    assert f'name="next" value="{origin}"'.encode() in response.content


def test_signing_out_and_choosing_a_language_need_no_confirmation() -> None:
    client, user = verified_admin(confirmed=False)

    language = client.post(
        reverse("admin:workspace_language"), {"language": "tr", "next": "/admin/"}
    )
    signed_out = client.post(reverse("admin:logout"))

    assert language.status_code == 302
    assert UserLanguagePreference.objects.get(user=user).language == Language.TR
    assert signed_out.status_code == 302
    assert client.get("/api/session/").json()["user"] is None
