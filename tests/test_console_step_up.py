import re
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
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
from pyracast.languages import LANGUAGE_COOKIE
from pyracast.notifications.models import NotificationSettings
from tests.support.api import verified_admin
from tests.support.auth_lifecycle import totp_code
from tests.support.step_up import with_proof

pytestmark = pytest.mark.django_db
CONFIRMATION = "admin/workspace/confirm.html"
# Signing out and confirming with the authenticator are the console's only writes that need no
# recent confirmation. Django's console login route stores nothing and only redirects.
UNGUARDED = {"logout", "workspace_confirm"}
REDIRECTS = {"login"}
SAMPLES = {"app_label": "regions", "url": "unknown/"}
SESSION_TABLES = ('"django_session"', '"usersessions_usersession"')
BOUND = settings.ACCOUNT_REAUTHENTICATION_TIMEOUT
STALE = ["missing", "expired"]


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
    assert {name for _route, name in ROUTES} >= UNGUARDED | REDIRECTS


def stale_admin(evidence: str) -> tuple[Client, User]:
    client, user = verified_admin(confirmed=False)
    if evidence == "expired":
        with_proof(client, user, "mfa", age=BOUND, reauthenticated=True, **factor_of(user))
    return client, user


@pytest.mark.parametrize("evidence", STALE)
@pytest.mark.parametrize(("route", "name"), ROUTES, ids=[route for route, _ in ROUTES])
def test_a_console_write_without_recent_mfa_evidence_is_refused_and_stores_nothing(
    route: str, name: str, evidence: str
) -> None:
    client, _ = stale_admin(evidence)
    client.get("/admin/")

    with CaptureQueriesContext(connection) as queries:
        response = client.post(concrete(route), {"action": "approve", "language": "tr"})

    if name in REDIRECTS:
        assert response.status_code == 302
        assert writes(queries) == []
    elif name in UNGUARDED:
        assert not refused(response)
    else:
        assert refused(response)
        assert writes(queries) == []


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_every_unsafe_method_needs_recent_mfa_evidence(method: str) -> None:
    client, _ = verified_admin(confirmed=False)

    assert refused(getattr(client, method)(reverse("admin:workspace_settings")))


@pytest.mark.parametrize("evidence", STALE)
def test_a_stale_console_write_changes_nothing_and_a_confirmed_one_saves(evidence: str) -> None:
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 5})
    client, user = stale_admin(evidence)
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
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 5})
    client, user = verified_admin(confirmed=False)
    extra = factor_of(user) if method == "mfa" else {}
    RecoveryCodes.activate(user)
    if evidence != "sign-in only":
        with_proof(client, user, method, age=age, reauthenticated=reauthenticated, **extra)

    api = client.get("/api/auth/browser/v1/account/authenticators/recovery-codes")
    console = client.post(
        reverse("admin:workspace_settings"), {"max_recipients": 8, "expected_max_recipients": 5}
    )

    if recent:
        assert api.status_code == 200
        assert console.status_code == 302
        assert NotificationSettings.objects.get().max_recipients == 8
    else:
        assert api.status_code == 401
        assert [error["code"] for error in api.json()["errors"]] == [
            "mfa_reauthentication_required"
        ]
        assert refused(console)
        assert NotificationSettings.objects.get().max_recipients == 5


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


def recovery_codes_removed(user: User) -> None:
    Authenticator.objects.filter(user=user, type="recovery_codes").delete()


def recovery_codes_reset(user: User) -> None:
    recovery_codes_removed(user)
    RecoveryCodes.activate(user)


def password_set(user: User) -> None:
    user.set_password("operator-only-" + "harbour-lantern-58")
    user.save(update_fields=["password"])


def address_added(user: User) -> None:
    EmailAddress.objects.create(user=user, email="elsewhere@example.com", verified=False)


# Credential changes made outside the confirmed session; only a password change ends the session.
CONSOLE_CHANGES: dict[str, Callable[[User], object]] = {
    "password-set": password_set,
    "factor-added": RecoveryCodes.activate,
    "factor-removed": recovery_codes_removed,
    "factor-reset": recovery_codes_reset,
    "address-added": address_added,
}


@CODE_LIMITS
@pytest.mark.parametrize("change", ["unchanged", *CONSOLE_CHANGES])
def test_a_console_confirmation_holds_only_while_the_credential_state_it_was_given_under_holds(
    operator: tuple[Client, str, float], change: str
) -> None:
    client, secret, now = operator
    user = User.objects.get(username="operator")
    if change != "factor-added":
        RecoveryCodes.activate(user)
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 5})
    settings_path = reverse("admin:workspace_settings")
    confirmed = client.post(
        reverse("admin:workspace_confirm"), {"code": totp_code(secret, now), "next": settings_path}
    )
    assert confirmed.status_code == 302
    if change != "unchanged":
        CONSOLE_CHANGES[change](user)

    response = client.post(settings_path, {"max_recipients": 8, "expected_max_recipients": 5})

    if change == "unchanged":
        assert response.status_code == 302
        assert NotificationSettings.objects.get().max_recipients == 8
        return
    assert NotificationSettings.objects.get().max_recipients == 5
    assert not LogEntry.objects.exists()
    if change == "password-set":
        # A password change ends every session of the account, and the confirmation with it.
        assert client.get("/api/session/").json()["user"] is None
        return
    assert refused(response)
    assert client.get(reverse("admin:index")).status_code == 200


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


@pytest.mark.parametrize("method", ["put", "delete"])
def test_the_confirmation_answers_only_get_and_post(method: str) -> None:
    client, _ = verified_admin(confirmed=False)

    assert getattr(client, method)(reverse("admin:workspace_confirm")).status_code == 405


def test_a_refused_write_offers_to_return_to_the_page_it_came_from() -> None:
    client, _ = verified_admin(confirmed=False)
    origin = reverse("admin:workspace_settings")

    response = client.post(origin, {}, HTTP_REFERER=f"http://testserver{origin}")

    assert refused(response)
    assert f'name="next" value="{origin}"'.encode() in response.content


def test_a_language_choice_is_a_confirmed_write_and_signing_out_needs_no_confirmation() -> None:
    client, user = verified_admin(confirmed=False)
    choice = {"language": "tr", "next": "/admin/"}

    stale = client.post(reverse("admin:workspace_language"), choice)
    assert refused(stale)
    assert LANGUAGE_COOKIE not in stale.cookies
    assert not UserLanguagePreference.objects.filter(user=user).exists()
    with_proof(client, user, "mfa", age=0, reauthenticated=True, **factor_of(user))
    confirmed = client.post(reverse("admin:workspace_language"), choice)
    signed_out = client.post(reverse("admin:logout"))

    assert confirmed.status_code == 302
    assert UserLanguagePreference.objects.get(user=user).language == Language.TR
    assert signed_out.status_code == 302
    assert client.get("/api/session/").json()["user"] is None
