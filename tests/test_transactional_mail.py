import re
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.template import TemplateDoesNotExist
from django.test import override_settings
from django.utils import dateformat, timezone, translation
from django.utils.html import escape

from pyracast.accounts.adapters import AccountAdapter
from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import (
    Membership,
    Tenant,
    UserLanguagePreference,
    WorkspaceInvitation,
    WorkspaceRole,
)
from tests.support.account_mail import TIME_FORMATS, TURKIYE
from tests.support.outbox import deliver_mail


def test_unknown_account_mail_template_is_not_rendered_as_a_code_message() -> None:
    with pytest.raises(TemplateDoesNotExist):
        AccountAdapter().render_mail("account/email/unsupported", "member@example.com", {})


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("kind", ["email_confirmation_signup", "password_reset_code", "login_code"])
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
            },
        )

    assert isinstance(message, EmailMultiAlternatives)
    assert message.to == ["member@example.com"]
    assert "012345" in str(message.body)
    assert len(message.alternatives) == 1
    html, mimetype = message.alternatives[0]
    assert isinstance(html, str)
    assert mimetype == "text/html"
    assert f'<html lang="{language}" dir="ltr">' in html
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


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("language", ["en", "tr"])
@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_invitation_mail_has_private_action_and_escaped_workspace(language: str) -> None:
    tenant = Tenant.objects.create(name='<img src=x onerror="unsafe"> & Solar')
    actor = User.objects.create_user(username="administrator", email="admin@example.com")
    Membership.objects.create(tenant=tenant, user=actor, role=WorkspaceRole.ADMIN)
    UserLanguagePreference.objects.create(user=actor, language=language)

    invitation = invite_workspace_member(actor, tenant.pk, "guest@example.com")

    deliver_mail()
    message = mail.outbox[-1]
    assert isinstance(message, EmailMultiAlternatives)
    invitation.refresh_from_db()
    assert invitation.delivery_status == "sent"
    assert message.to == ["guest@example.com"]
    assert tenant.name in str(message.body)
    html = message.alternatives[0][0]
    assert isinstance(html, str)
    assert f'<html lang="{language}" dir="ltr">' in html
    assert '<img src=x onerror="unsafe">' not in html
    assert "&lt;img" in html
    assert "/app/invitation/#token=" in html
    assert "?token=" not in html
    assert ("Daveti incele" if language == "tr" else "Review invitation") in html


INVITATION_TEXT = {
    "en": (
        "Workspace invitation",
        "You are invited to Solar.",
        "Invited role: Member\nInvitation expires: {deadline} (UTC+3)",
        "Sign in or create an account with guest@example.com, then verify your email to "
        "accept the invitation.",
        "Review invitation: ",
    ),
    "tr": (
        "Çal\u0131şma alan\u0131 daveti",
        "Solar çal\u0131şma alan\u0131na davet edildiniz.",
        "Davet edildiğiniz rol: Üye\nDavetin son geçerlilik zaman\u0131: {deadline} (UTC+3)",
        "guest@example.com adresiyle giriş yap\u0131n veya hesap oluşturun, "
        "ard\u0131ndan daveti kabul "
        "etmek için e-posta adresinizi doğrulay\u0131n.",
        "Daveti incele: ",
    ),
}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("language", ["en", "tr"])
@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_invitation_mail_names_the_role_and_the_stored_deadline_in_both_parts(
    language: str,
) -> None:
    tenant = Tenant.objects.create(name="Solar")
    actor = User.objects.create_user(username="administrator", email="admin@example.com")
    Membership.objects.create(tenant=tenant, user=actor, role=WorkspaceRole.ADMIN)
    invitation = invite_workspace_member(actor, tenant.pk, "guest@example.com", language=language)
    deadline = (timezone.now() + timedelta(days=3)).replace(second=0, microsecond=0)
    WorkspaceInvitation.objects.filter(pk=invitation.pk).update(expires_at=deadline)

    deliver_mail()

    message = mail.outbox[-1]
    with translation.override(language):
        shown = dateformat.format(deadline.astimezone(TURKIYE), TIME_FORMATS[language])
    heading, invited, lines, step, action = INVITATION_TEXT[language]
    lines = lines.format(deadline=shown)
    assert str(message.body).startswith(
        "\n\n".join(["Pyracast", heading, invited, lines, step, action])
    )
    assert isinstance(message, EmailMultiAlternatives)
    html = message.alternatives[0][0]
    assert isinstance(html, str)
    assert re.findall(r"<h1[^>]*>(.*?)</h1>", html) == [escape(heading)]
    shown_lines = [escape(line).replace(": ", ":</strong> ", 1) for line in lines.split("\n")]
    position = html.index("</h1>")
    for part in [escape(invited), *shown_lines, escape(step), escape(action.removesuffix(": "))]:
        found = html.find(part, position)
        assert found >= 0, part
        position = found + len(part)
