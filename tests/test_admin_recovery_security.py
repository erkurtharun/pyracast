import time
from typing import Any

import pytest
from allauth.account.internal.flows.login import get_authentication_records, record_authentication
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP, format_hotp_value, generate_totp_secret, hotp_value
from django.contrib.auth.models import AnonymousUser, User
from django.test import Client, RequestFactory, override_settings

from pyracast.accounts.mfa_views import recently_reauthenticated_with_mfa
from tests.test_admin_mfa_bootstrap import TEST_PASSWORD, administrator

pytestmark = pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
AUTH = "/api/auth/browser/v1/"
RECOVERY = AUTH + "account/authenticators/recovery-codes"


def submit(client: Client, path: str, data: dict[str, Any]) -> Any:
    token = client.get("/api/session/").json()["csrf_token"]
    return client.post(path, data, content_type="application/json", HTTP_X_CSRFTOKEN=token)


def authenticated_administrator() -> tuple[Client, User, str]:
    user = administrator()
    secret = generate_totp_secret()
    authenticator = TOTP.activate(user, secret).instance
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session
    record_authentication(request, user, method="mfa", type="totp", id=authenticator.pk)
    request.session.save()
    return client, user, secret


@pytest.mark.parametrize("method", ["get", "post"])
def test_stolen_admin_session_and_password_cannot_read_or_replace_recovery_codes(
    method: str,
) -> None:
    client, user, _ = authenticated_administrator()
    recovery = RecoveryCodes.activate(user).instance
    original = recovery.data.copy()
    assert (
        submit(client, AUTH + "auth/reauthenticate", {"password": TEST_PASSWORD}).status_code == 200
    )
    response = client.get(RECOVERY) if method == "get" else submit(client, RECOVERY, {})
    assert response.status_code == 401
    assert response.json()["errors"][0]["code"] == "mfa_reauthentication_required"
    assert response.json()["meta"]["is_authenticated"]
    recovery.refresh_from_db()
    assert recovery.data == original
    assert Authenticator.objects.filter(user=user, type="recovery_codes").count() == 1


@pytest.mark.parametrize("factor", ["totp", "recovery_codes"])
@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_fresh_mfa_reauthentication_allows_administrator_recovery_code_rotation(
    factor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, user, secret = authenticated_administrator()
    recovery = RecoveryCodes.activate(user)
    original = recovery.generate_codes()
    now = time.time()
    code = (
        format_hotp_value(hotp_value(secret, int(now) // 30)) if factor == "totp" else original[0]
    )
    assert submit(client, AUTH + "auth/2fa/reauthenticate", {"code": code}).status_code == 200
    response = submit(client, RECOVERY, {})
    assert response.status_code == 200
    assert response.json()["data"]["unused_codes"] != original
    viewed = client.get(RECOVERY)
    if factor == "totp":
        assert "unused_codes" not in viewed.json()["data"]
    else:
        assert viewed.status_code == 401

    monkeypatch.setattr(time, "time", lambda: now + 301)
    assert (
        submit(client, AUTH + "auth/reauthenticate", {"password": TEST_PASSWORD}).status_code == 200
    )
    assert submit(client, RECOVERY, {}).status_code == 401


@pytest.mark.parametrize("invalid_at", [None, "invalid", float("nan"), float("inf"), -1])
def test_invalid_mfa_reauthentication_timestamp_cannot_authorize_recovery_codes(
    invalid_at: object,
) -> None:
    client, user, _ = authenticated_administrator()
    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session
    record = get_authentication_records(request, user)[0]
    record["reauthenticated"] = True
    record["at"] = invalid_at
    assert not recently_reauthenticated_with_mfa(request)


def test_deleted_mfa_factor_cannot_authorize_recovery_codes() -> None:
    client, user, _ = authenticated_administrator()
    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session
    get_authentication_records(request, user)[0]["reauthenticated"] = True
    Authenticator.objects.filter(user=user).delete()
    assert not recently_reauthenticated_with_mfa(request)


def test_anonymous_request_has_no_mfa_reauthentication() -> None:
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    assert not recently_reauthenticated_with_mfa(request)
