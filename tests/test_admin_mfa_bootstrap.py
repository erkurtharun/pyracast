from io import StringIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
import segno
from allauth.account.internal.flows.login import record_authentication
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, RequestFactory

from pyracast.accounts.mfa import MFAAdapter

pytestmark = pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
TEST_PASSWORD = "test-only-" + "z" * 40


def administrator() -> User:
    user = User.objects.create_user(
        username="operator",
        email="operator@example.com",
        password=TEST_PASSWORD,
        is_staff=True,
        is_superuser=True,
    )
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    return user


def test_admin_password_alone_cannot_bootstrap_an_authenticator() -> None:
    user = administrator()
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/session/").json()["csrf_token"]
    response = client.post(
        "/api/auth/browser/v1/auth/login",
        {"email": user.email, "password": TEST_PASSWORD},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 400
    assert client.get("/api/session/").json()["user"] is None
    assert not Authenticator.objects.filter(user=user).exists()
    assert client.get("/admin/").status_code == 302


@pytest.mark.parametrize("method", ["get", "post"])
def test_authenticated_admin_cannot_use_customer_totp_enrollment(method: str) -> None:
    user = administrator()
    client = Client()
    client.force_login(user)
    response = getattr(client, method)("/api/auth/browser/v1/account/authenticators/totp")
    assert response.status_code == 403
    assert not Authenticator.objects.filter(user=user).exists()


def test_trusted_command_provisions_encrypted_factor_and_never_replaces_it() -> None:
    user = administrator()
    output = StringIO()
    with patch(
        "pyracast.accounts.management.commands.provision_admin_mfa.segno.make_qr",
        wraps=segno.make_qr,
    ) as encode:
        call_command("provision_admin_mfa", username=user.username, stdout=output)
    uri = output.getvalue().splitlines()[-1]
    encode.assert_called_once_with(uri)
    assert "Scan this private QR code" in output.getvalue()
    assert any(block in output.getvalue() for block in "\u2580\u2584\u2588")
    assert "\x1b" not in output.getvalue()
    secret = parse_qs(urlsplit(uri).query)["secret"][0]
    authenticator = Authenticator.objects.get(user=user, type="totp")
    assert secret not in authenticator.data["secret"]
    assert MFAAdapter().decrypt(authenticator.data["secret"]) == secret
    rejected_output = StringIO()
    with pytest.raises(CommandError, match="never replaces"):
        call_command("provision_admin_mfa", username=user.username, stdout=rejected_output)
    assert rejected_output.getvalue() == ""
    assert Authenticator.objects.filter(user=user).count() == 1


def test_provisioning_retains_uri_fallback_when_console_cannot_encode_qr() -> None:
    class AsciiConsole(StringIO):
        def write(self, value: str) -> int:
            value.encode("ascii")
            return super().write(value)

    user = administrator()
    output = AsciiConsole()
    call_command("provision_admin_mfa", username=user.username, stdout=output)
    assert "This console cannot display the QR code" in output.getvalue()
    uri = output.getvalue().splitlines()[-1]
    secret = parse_qs(urlsplit(uri).query)["secret"][0]
    authenticator = Authenticator.objects.get(user=user, type="totp")
    assert MFAAdapter().decrypt(authenticator.data["secret"]) == secret


@pytest.mark.parametrize("kind", ["missing", "customer", "inactive", "staff", "superuser"])
def test_trusted_command_rejects_ineligible_accounts(kind: str) -> None:
    if kind != "missing":
        User.objects.create_user(
            username="ineligible",
            is_active=kind != "inactive",
            is_staff=kind in {"inactive", "staff"},
            is_superuser=kind in {"inactive", "superuser"},
        )
    with pytest.raises(CommandError, match="active administrator"):
        call_command("provision_admin_mfa", username="ineligible", stdout=StringIO())
    assert not Authenticator.objects.exists()


def test_verified_admin_uses_common_password_change_and_strict_csp() -> None:
    user = administrator()
    authenticator = Authenticator.objects.create(user=user, type="totp", data={})
    client = Client()
    client.force_login(user)
    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session
    record_authentication(request, user, method="mfa", type="totp", id=authenticator.pk)
    request.session.save()
    response = client.get("/admin/")
    assert response.status_code == 200
    policy = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in policy
    assert "object-src 'none'" in policy
    assert "unsafe-inline" not in policy
    assert client.get("/admin/login/").headers["Location"] == "/admin/"
    assert client.get("/admin/password_change/").headers["Location"] == "/app/?account=security"


def test_legacy_administrator_requires_explicit_email_and_proves_ownership_later() -> None:
    user = User.objects.create_user(username="legacy", is_staff=True, is_superuser=True)
    with pytest.raises(CommandError, match="--email is required"):
        call_command("provision_admin_mfa", username=user.username, stdout=StringIO())
    assert not Authenticator.objects.exists()
    call_command(
        "provision_admin_mfa", username=user.username, email="Owner@Example.com", stdout=StringIO()
    )
    user.refresh_from_db()
    assert user.email == "owner@example.com"
    identity = EmailAddress.objects.get(user=user)
    assert identity.primary
    assert not identity.verified
    assert Authenticator.objects.filter(user=user, type="totp").exists()


@pytest.mark.parametrize("registered", [False, True])
def test_conflicting_email_cannot_leave_an_orphaned_administrator_factor(registered: bool) -> None:
    user = User.objects.create_user(username="legacy", is_staff=True, is_superuser=True)
    other = User.objects.create_user(username="other", email="owner@example.com")
    if registered:
        EmailAddress.objects.create(user=other, email=other.email, verified=True, primary=True)
    with pytest.raises(CommandError, match="another account"):
        call_command(
            "provision_admin_mfa", username=user.username, email=other.email, stdout=StringIO()
        )
    user.refresh_from_db()
    assert user.email == ""
    assert not EmailAddress.objects.filter(user=user).exists()
    assert not Authenticator.objects.exists()


@pytest.mark.parametrize("email", ["invalid", "x" * 245 + "@example.com"])
def test_provisioning_rejects_invalid_email_without_changes(email: str) -> None:
    user = User.objects.create_user(username="legacy", is_staff=True, is_superuser=True)
    with pytest.raises(CommandError, match="valid email"):
        call_command("provision_admin_mfa", username=user.username, email=email, stdout=StringIO())
    assert not EmailAddress.objects.exists()
    assert not Authenticator.objects.exists()


def test_provisioning_does_not_replace_an_existing_email_identity() -> None:
    user = administrator()
    with pytest.raises(CommandError, match="never replaces"):
        call_command(
            "provision_admin_mfa",
            username=user.username,
            email="new@example.com",
            stdout=StringIO(),
        )
    assert EmailAddress.objects.get(user=user).email == "operator@example.com"
    assert not Authenticator.objects.exists()


def test_provisioning_requires_resolution_of_nonprimary_email_identity() -> None:
    user = User.objects.create_user(username="legacy", is_staff=True, is_superuser=True)
    EmailAddress.objects.create(user=user, email="old@example.com", verified=False, primary=False)
    with pytest.raises(CommandError, match="Resolve the existing"):
        call_command(
            "provision_admin_mfa",
            username=user.username,
            email="new@example.com",
            stdout=StringIO(),
        )
    assert not Authenticator.objects.exists()
