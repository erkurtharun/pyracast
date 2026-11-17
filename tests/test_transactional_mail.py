from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.test import override_settings
from django.utils import translation

from pyracast.accounts.adapters import AccountAdapter
from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import Membership, Tenant, UserLanguagePreference


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("kind", ["email_confirmation_signup", "password_reset_code", "login_code"])
@override_settings(ACCOUNT_EMAIL_SUBJECT_PREFIX="[Pyracast] ")
def test_authentication_code_mail_has_accessible_html_and_matching_plain_text(
    language: str, kind: str
) -> None:
    with translation.override(language):
        message = AccountAdapter().render_mail(
            f"account/email/{kind}",
            "member@example.com",
            {
                "code": "012345",
                "user": User(username="member"),
                "current_site": SimpleNamespace(name="Pyracast", domain="pyracast.example"),
            },
        )

    assert isinstance(message, EmailMultiAlternatives)
    assert message.to == ["member@example.com"]
    assert "012345" in str(message.body)
    assert len(message.alternatives) == 1
    html, mimetype = message.alternatives[0]
    assert isinstance(html, str)
    assert mimetype == "text/html"
    assert f'<html lang="{language}">' in html
    assert html.count("012345") == 1
    assert "<h1" in html
    assert "<script" not in html
    assert "<button" not in html
    assert "10" in html
    assert ("dakika" if language == "tr" else "minutes") in html


@pytest.mark.parametrize(
    ("kind", "minutes"),
    [("email_confirmation", 30), ("password_reset_code", 20), ("login_code", 5)],
)
@override_settings(
    ACCOUNT_EMAIL_SUBJECT_PREFIX="[Pyracast] ",
    ACCOUNT_EMAIL_VERIFICATION_BY_CODE_TIMEOUT=1800,
    ACCOUNT_PASSWORD_RESET_BY_CODE_TIMEOUT=1200,
    ACCOUNT_LOGIN_BY_CODE_TIMEOUT=300,
)
def test_code_mail_expiry_matches_the_configured_flow(kind: str, minutes: int) -> None:
    with translation.override("en"):
        message = AccountAdapter().render_mail(
            f"account/email/{kind}", "member@example.com", {"code": "012345"}
        )

    assert f"{minutes} minutes" in str(message.body)
    assert f"{minutes} minutes" in str(message.alternatives[0][0])


@pytest.mark.parametrize(
    ("kind", "url_key"),
    [("email_confirmation", "activate_url"), ("password_reset_key", "password_reset_url")],
)
@override_settings(ACCOUNT_EMAIL_SUBJECT_PREFIX="[Pyracast] ")
def test_authentication_link_mail_has_real_action_and_preserves_token(
    kind: str, url_key: str
) -> None:
    link = "https://pyracast.example/confirm?key=abc&next=%2Fsettings#token=test"
    message = AccountAdapter().render_mail(
        f"account/email/{kind}", "member@example.com", {url_key: link}
    )

    assert link in str(message.body)
    html = message.alternatives[0][0]
    assert isinstance(html, str)
    assert html.count('href="' + link.replace("&", "&amp;") + '"') == 2
    assert "<button" not in html


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("language", ["en", "tr"])
@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_invitation_mail_has_private_action_and_escaped_workspace(language: str) -> None:
    tenant = Tenant.objects.create(name='<img src=x onerror="unsafe"> & Solar')
    actor = User.objects.create_user(username="administrator", email="admin@example.com")
    Membership.objects.create(tenant=tenant, user=actor, role=Membership.Role.ADMIN)
    UserLanguagePreference.objects.create(user=actor, language=language)

    invitation = invite_workspace_member(actor, tenant.pk, "guest@example.com")

    message = mail.outbox[-1]
    assert isinstance(message, EmailMultiAlternatives)
    assert invitation.delivery_status == "sent"
    assert message.to == ["guest@example.com"]
    assert tenant.name in str(message.body)
    html = message.alternatives[0][0]
    assert isinstance(html, str)
    assert f'<html lang="{language}">' in html
    assert '<img src=x onerror="unsafe">' not in html
    assert "&lt;img" in html
    assert "/app/#token=" in html
    assert "?token=" not in html
    assert ("Daveti incele" if language == "tr" else "Review invitation") in html


@pytest.mark.parametrize(
    "prefix",
    [
        "account/email/email_changed",
        "account/email/email_confirm",
        "account/email/email_deleted",
        "account/email/password_changed",
        "account/email/password_set",
        "mfa/email/recovery_codes_generated",
        "mfa/email/totp_activated",
        "mfa/email/totp_deactivated",
        "mfa/email/webauthn_added",
        "mfa/email/webauthn_removed",
    ],
)
@override_settings(ACCOUNT_EMAIL_SUBJECT_PREFIX="[Pyracast] ")
def test_security_mail_retains_event_context_and_escapes_html(prefix: str) -> None:
    with translation.override("en"):
        message = AccountAdapter().render_mail(
            prefix,
            "member@example.com",
            {
                "current_site": SimpleNamespace(name="Pyracast", domain="pyracast.example"),
                "user": User(username="member"),
                "email": "member@example.com",
                "ip": "192.0.2.4",
                "user_agent": '<script>alert("unsafe")</script>',
                "timestamp": "2026-11-15 12:00 UTC",
            },
        )

    assert isinstance(message, EmailMultiAlternatives)
    assert len(message.alternatives) == 1
    html = message.alternatives[0][0]
    assert isinstance(html, str)
    for content in ("192.0.2.4", "2026-11-15 12:00 UTC"):
        assert content in str(message.body)
        assert content in html
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
