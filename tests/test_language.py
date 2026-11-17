from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from django.conf import LazySettings
from django.contrib.auth.models import User
from django.core import mail
from django.core.mail import EmailMessage
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.http import HttpResponse
from django.test import Client, RequestFactory, override_settings
from django.utils import translation

from pyracast.accounts.adapters import AccountAdapter
from pyracast.accounts.language import (
    LANGUAGE_COOKIE,
    Language,
    RequestLanguage,
    browser_language,
    save_user_language,
)
from pyracast.accounts.models import UserLanguagePreference
from pyracast.admin_workspace import recent_activity_context
from pyracast.regions.models import AuditEvent
from tests.support.admin_setup import admin_system
from tests.support.api import verified_admin
from tests.support.company_registration import signup

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def choose(client: Client, language: object, **extra: Any) -> Any:
    token = client.get("/api/session/").json()["csrf_token"]
    return client.post(
        "/api/session/language/",
        {"language": language, **extra},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("", "en"),
        ("tr-TR,tr;q=0.9,en;q=0.8", "tr"),
        ("en-US;q=0.9,tr;q=0.8", "en"),
        ("de,TR-tr;q=0.8,en;q=0.7", "tr"),
        ("tr;q=0,en;q=0.2", "en"),
        ("en;q=0,tr;q=0.2", "tr"),
        ("tr;q=0.4,en;q=0.4", "tr"),
        ("de-DE,fr;q=0.8", "en"),
        ("*", "en"),
        ("tr;q=2,en;q=0.2", "en"),
        ("tr;q=NaN,en;q=0.2", "en"),
        ("tr;q=-1,en;q=0.2", "en"),
        ("tr;q=0.1234,en;q=0.2", "en"),
        ("tr;q=1;broken,en;q=0.2", "en"),
        ("tr_ TR,en;q=0.2", "en"),
        ("tr;q=0.000,en;q=1.000", "en"),
    ],
)
def test_browser_language_priorities(header: str, expected: str) -> None:
    response = Client().get("/api/session/", HTTP_ACCEPT_LANGUAGE=header)
    assert response.json()["language"] == response.headers["Content-Language"] == expected
    assert "no-store" in response.headers["Cache-Control"]
    assert {"Cookie", "Accept-Language"} <= set(response.headers["Vary"].split(", "))
    assert LANGUAGE_COOKIE not in response.cookies


@pytest.mark.parametrize("cookie", ["tr", "en", "unsupported", "TR"])
def test_explicit_cookie_and_profile_precedence(cookie: str) -> None:
    client = Client()
    client.cookies[LANGUAGE_COOKIE] = cookie
    expected = cookie if cookie in {"en", "tr"} else "tr"
    assert client.get("/api/session/", HTTP_ACCEPT_LANGUAGE="tr").json()["language"] == expected
    user = User.objects.create_user(username="owner")
    client.force_login(user)
    assert client.get("/api/session/").json()["user"]["language_preference"] is None
    UserLanguagePreference.objects.create(user=user, language="en")
    response = client.get("/api/session/", HTTP_ACCEPT_LANGUAGE="tr")
    assert response.json()["language"] == "en"
    assert response.json()["user"]["language_preference"] == "en"
    if cookie == "en":
        assert LANGUAGE_COOKIE not in response.cookies
    else:
        assert response.cookies[LANGUAGE_COOKIE].value == "en"
        assert response.cookies[LANGUAGE_COOKIE]["httponly"]


def test_language_choice_with_stale_cookie_keeps_the_new_value() -> None:
    client = Client(enforce_csrf_checks=True)
    user = User.objects.create_user(username="owner")
    UserLanguagePreference.objects.create(user=user, language="en")
    client.force_login(user)
    token = client.get("/api/session/").json()["csrf_token"]
    client.cookies[LANGUAGE_COOKIE] = "de"
    response = client.post(
        "/api/session/language/",
        {"language": "tr"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.json()["user"]["language_preference"] == "tr"
    assert response.cookies[LANGUAGE_COOKIE].value == "tr"
    assert UserLanguagePreference.objects.get(user=user).language == "tr"


def test_anonymous_choice_requires_csrf_and_sets_only_explicit_cookie() -> None:
    client = Client(enforce_csrf_checks=True)
    rejected = client.post(
        "/api/session/language/", {"language": "tr"}, content_type="application/json"
    )
    assert rejected.status_code == 403
    assert LANGUAGE_COOKIE not in rejected.cookies
    accepted = choose(client, "tr")
    assert accepted.status_code == 200
    assert accepted.json()["language"] == accepted.headers["Content-Language"] == "tr"
    assert accepted.json()["user"] is None
    assert accepted.json()["csrf_token"]
    cookie = accepted.cookies[LANGUAGE_COOKIE]
    assert cookie.value == "tr"
    assert cookie["secure"]
    assert cookie["httponly"]
    assert cookie["samesite"] == "Lax"
    assert cookie["path"] == "/"
    assert cookie["max-age"] == 365 * 24 * 60 * 60
    assert not UserLanguagePreference.objects.exists()
    assert client.get("/api/session/", HTTP_ACCEPT_LANGUAGE="en").json()["language"] == "tr"
    with override_settings(DEBUG=True):
        assert not choose(client, "en").cookies[LANGUAGE_COOKIE]["secure"]


@pytest.mark.parametrize("language", ["de", "TR", "tr-TR", None, 1, ["tr"], ""])
def test_unsupported_language_does_not_write(language: object) -> None:
    response = choose(Client(enforce_csrf_checks=True), language)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert LANGUAGE_COOKIE not in response.cookies
    assert not UserLanguagePreference.objects.exists()


def test_authenticated_preference_survives_new_device_and_is_owned_by_caller() -> None:
    owner, other = (
        User.objects.create_user(username="owner"),
        User.objects.create_user(username="other"),
    )
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner)
    assert (
        client.post(
            "/api/session/language/", {"language": "tr"}, content_type="application/json"
        ).status_code
        == 403
    )
    assert choose(client, "tr", user_id=other.pk).status_code == 400
    assert not UserLanguagePreference.objects.exists()
    response = choose(client, "tr")
    assert response.json()["user"]["id"] == owner.pk
    assert response.json()["user"]["language_preference"] == "tr"
    assert not UserLanguagePreference.objects.filter(user=other).exists()
    device = Client()
    device.force_login(owner)
    device.cookies[LANGUAGE_COOKIE] = "en"
    assert device.get("/api/session/", HTTP_ACCEPT_LANGUAGE="en").json()["language"] == "tr"
    assert choose(client, "en").json()["user"]["language_preference"] == "en"
    assert UserLanguagePreference.objects.filter(user=owner).count() == 1
    device.force_login(other)
    assert device.get("/api/session/").json()["user"]["language_preference"] is None


@pytest.mark.parametrize(
    ("peer", "chain", "country", "browser", "expected"),
    [
        ("172.30.80.2", "198.51.100.7, 173.245.48.1", "TR", "", "tr"),
        ("172.30.80.2", "198.51.100.7, 173.245.48.1", "US", "", "en"),
        ("172.30.80.2", "198.51.100.7, 173.245.48.1", "TR", "en", "en"),
        ("172.30.80.2", "198.51.100.7, 173.245.48.1", "TR", "tr;q=0", "en"),
        ("172.30.80.2", "198.51.100.7, 173.245.48.1", "TR", "*;q=0", "en"),
        ("172.30.80.2", "198.51.100.7", "TR", "", "en"),
        ("198.51.100.7", "198.51.100.7, 173.245.48.1", "TR", "", "en"),
        ("172.30.80.2", "198.51.100.7, 173.245.48.1", "", "", "en"),
    ],
)
def test_country_is_used_only_from_trusted_upstream(
    settings: LazySettings, peer: str, chain: str, country: str, browser: str, expected: str
) -> None:
    settings.PYRACAST_TRUST_PROXY = True
    settings.PYRACAST_TRUSTED_PROXY_CIDRS = ["172.30.80.2/32"]
    settings.PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS = ["173.245.48.0/20"]
    response = Client().get(
        "/api/session/",
        REMOTE_ADDR=peer,
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_X_REAL_IP="173.245.48.1",
        HTTP_X_FORWARDED_FOR=chain,
        HTTP_CF_IPCOUNTRY=country,
        HTTP_ACCEPT_LANGUAGE=browser,
    )
    assert response.json()["language"] == expected


def test_direct_country_header_and_gateway_without_upstream_are_ignored(
    settings: LazySettings,
) -> None:
    assert Client().get("/api/session/", HTTP_CF_IPCOUNTRY="TR").json()["language"] == "en"
    settings.PYRACAST_TRUST_PROXY = True
    settings.PYRACAST_TRUSTED_PROXY_CIDRS = ["127.0.0.1/32"]
    settings.PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS = []
    response = Client().get(
        "/api/session/",
        HTTP_CF_IPCOUNTRY="TR",
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_X_REAL_IP="1.1.1.1",
    )
    assert response.json()["language"] == "en"


@pytest.mark.parametrize("cookie", ["tr", "en", "unsupported", None])
@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_signup_saves_the_form_language_as_preference(cookie: str | None) -> None:
    client = Client(enforce_csrf_checks=True, HTTP_ACCEPT_LANGUAGE="tr")
    if cookie is not None:
        client.cookies[LANGUAGE_COOKIE] = cookie
    assert signup(client).status_code == 401
    user = User.objects.get(email="owner@example.com")
    expected = cookie if cookie in {"tr", "en"} else "tr"
    assert UserLanguagePreference.objects.get(user=user).language == expected
    assert len(mail.outbox) == 1


def test_language_constraint_is_database_enforced() -> None:
    user = User.objects.create_user(username="owner")
    with pytest.raises(IntegrityError), transaction.atomic():
        UserLanguagePreference.objects.create(user=user, language="de")


def test_translation_scope_restores_previous_language() -> None:
    from django.contrib.auth.models import AnonymousUser

    request = RequestFactory().get("/", HTTP_ACCEPT_LANGUAGE="tr")
    request.user = AnonymousUser()

    def view(request: Any) -> HttpResponse:
        assert translation.get_language() == request.LANGUAGE_CODE == "tr"
        return HttpResponse(translation.gettext("Log in"))

    with translation.override("en"):
        response = RequestLanguage(view)(request)
        assert translation.get_language() == "en"
    assert response.content.decode() == "Oturum aç"
    assert browser_language("de;q=0") == (None, frozenset({"de"}))


def test_csrf_error_and_allauth_validation_use_selected_language() -> None:
    client = Client(enforce_csrf_checks=True, HTTP_ACCEPT_LANGUAGE="tr")
    response = client.post("/api/session/language/", {"language": "en"})
    assert response.status_code == 403
    assert response.headers["Content-Language"] == "tr"
    token = client.get("/api/session/").json()["csrf_token"]
    rejected = client.post(
        "/api/auth/browser/v1/auth/login",
        {"email": "invalid", "password": "invalid"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert rejected.status_code == 400
    assert any("geçerli" in error["message"].lower() for error in rejected.json()["errors"])


def test_concurrent_devices_create_one_account_preference() -> None:
    user = User.objects.create_user(username="owner")
    barrier = Barrier(2)

    def save(language: Language) -> None:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            save_user_language(user, language)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(save, "en")
        second = pool.submit(save, "tr")
        first.result(timeout=15)
        second.result(timeout=15)
    assert UserLanguagePreference.objects.filter(user=user).count() == 1
    assert UserLanguagePreference.objects.get(user=user).language in {"en", "tr"}


@pytest.mark.parametrize("preference", ["tr", None])
def test_authentication_mail_pins_language_until_transaction_commit(
    monkeypatch: pytest.MonkeyPatch, preference: str | None
) -> None:
    user = User.objects.create_user(username="owner")
    if preference:
        UserLanguagePreference.objects.create(user=user, language=preference)
    languages = []

    def capture(message: EmailMessage) -> None:
        languages.append(translation.get_language())

    monkeypatch.setattr("pyracast.accounts.adapters.send", capture)
    with transaction.atomic():
        with translation.override("en"):
            AccountAdapter().send_mail(
                "account/email/password_changed", "owner@example.com", {"user": user}
            )
        assert languages == []
    assert languages == [preference or "en"]


def test_admin_switcher_requires_csrf_and_uses_account_preference() -> None:
    system = admin_system()
    client, user = verified_admin(enforce_csrf_checks=True)
    endpoint = "/admin/language/"
    assert client.get(endpoint).status_code == 405
    assert client.post(endpoint, {"language": "tr"}).status_code == 403
    token = client.get("/api/session/").json()["csrf_token"]
    assert client.post(endpoint, {"language": "de"}, HTTP_X_CSRFTOKEN=token).status_code == 400
    response = client.post(
        endpoint,
        {"language": "tr", "next": "/admin/plants/?status=pending"},
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 302
    assert response.headers["Location"] == "/admin/plants/?status=pending"
    assert response.headers["Content-Language"] == "tr"
    assert response.cookies[LANGUAGE_COOKIE].value == "tr"
    assert UserLanguagePreference.objects.get(user=user).language == "tr"
    for path, heading in (
        ("/admin/", "Genel bak\u0131ş"),
        ("/admin/plants/", "Santraller"),
        ("/admin/operations/", "İşlemler"),
        ("/admin/settings/", "Ayarlar"),
        (f"/admin/plants/{system.region.pk}/", "Müşteri beyan\u0131"),
    ):
        page = client.get(path, HTTP_ACCEPT_LANGUAGE="en")
        assert page.status_code == 200
        text = page.content.decode()
        assert '<html lang="tr">' in text
        assert heading in text
        assert 'class="language-switcher"' in text
        assert 'value="tr" lang="tr" aria-pressed="true"' in text
        assert "no-store" in page.headers["Cache-Control"]
    assert Client().get("/admin/language/").status_code == 302


def test_admin_audit_activity_translates_known_events_and_keeps_unknown_fallback() -> None:
    system = admin_system()
    client, user = verified_admin()
    UserLanguagePreference.objects.create(user=user, language="tr")
    expected = {
        "connection_validated": "Sağlay\u0131c\u0131 bağlant\u0131s\u0131 doğruland\u0131",
        "region_renamed": "Santral konumu yeniden adland\u0131r\u0131ld\u0131",
        "collection_schedule_updated": "Veri toplama program\u0131 güncellendi",
        "historical_reference_imported": "Geçmiş referans verileri içe aktar\u0131ld\u0131",
        "reference_capacity_normalized": "Referans kapasitesi normalize edildi",
        "region_trashed": "Santral konumu çöp kutusuna taş\u0131nd\u0131",
        "region_restored": "Santral konumu geri yüklendi",
        "location_change_requested": "Konum değişikliği talep edildi",
        "future_platform_action": "Future platform action",
    }
    AuditEvent.objects.filter(region=system.region).delete()
    for action in expected:
        AuditEvent.objects.create(
            actor=system.member,
            region=system.region,
            action=action,
            revision=system.region.revision,
        )
    with translation.override("tr"):
        activity = recent_activity_context(system.region.pk, limit=len(expected))
    assert {event["action"]: event["action_label"] for event in activity} == expected
    response = client.get("/admin/")
    assert response.status_code == 200
    assert "Konum değişikliği talep edildi" in response.content.decode()
    assert "Location change requested" not in response.content.decode()
    assert "Future platform action" in response.content.decode()


@pytest.mark.parametrize(
    "destination",
    ["https://evil.example/path", "//evil.example", "\\\\evil.example", "http://testserver/admin/"],
)
def test_admin_language_switch_does_not_redirect_to_untrusted_target(destination: str) -> None:
    client, _ = verified_admin()
    response = client.post("/admin/language/", {"language": "en", "next": destination}, secure=True)
    assert response.status_code == 302
    assert response.headers["Location"] == "/admin/"
