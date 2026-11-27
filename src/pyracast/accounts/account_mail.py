from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from allauth.account.adapter import get_adapter
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import translation
from django.utils.functional import Promise
from django.utils.translation import gettext, pgettext_lazy
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.authentication import login_identity
from pyracast.accounts.language import user_language
from pyracast.accounts.models import OutboundMail, OutboundMailError, OutboundMailKind
from pyracast.notifications.formats import moment_text, utc_offset

# Accounts have no time zone of their own; their readers are in Türkiye.
ACCOUNT_ZONE = ZoneInfo("Europe/Istanbul")


@dataclass(frozen=True)
class Notice:
    subject: Promise
    sentence: Promise | None = None
    security: bool = True
    action_key: str = ""
    action_label: Promise | None = None


NOTICES = {
    "account/email/password_changed": Notice(_("Your password was changed")),
    "account/email/password_set": Notice(_("A password was set for your account")),
    "account/email/password_reset": Notice(_("Your password was reset")),
    "account/email/email_changed": Notice(
        _("Your email address was changed"),
        _("Your Pyracast email address changed from %(from_email)s to %(to_email)s."),
    ),
    "account/email/email_confirm": Notice(_("Your email address was verified")),
    "account/email/email_deleted": Notice(
        _("An email address was removed"),
        _("%(deleted_email)s was removed from your Pyracast account."),
    ),
    "mfa/email/totp_activated": Notice(_("An authenticator app was added to your account")),
    "mfa/email/totp_deactivated": Notice(_("An authenticator app was removed from your account")),
    OutboundMailKind.WEBAUTHN_ADDED: Notice(_("A passkey was added to your account")),
    OutboundMailKind.WEBAUTHN_REMOVED: Notice(_("A passkey was removed from your account")),
    "account/email/second_factor_reset": Notice(
        _("Pyracast removed your two-step verification"),
        _("You were signed out on every device. Sign in again."),
        security=False,
    ),
    "mfa/email/recovery_codes_generated": Notice(
        _("New recovery codes were generated"), _("The previous codes no longer work.")
    ),
    "socialaccount/email/account_connected": Notice(
        _("A %(provider)s account was connected"),
        _("You can now sign in to Pyracast with this account."),
    ),
    "socialaccount/email/account_disconnected": Notice(
        _("A %(provider)s account was disconnected"),
        _("You can no longer sign in to Pyracast with this account."),
    ),
    "account/email/unknown_account": Notice(
        _("Pyracast account not found"),
        _("Someone tried to use %(email)s with Pyracast, but no account uses this address."),
        security=False,
        action_key="signup_url",
        action_label=_("Create account"),
    ),
    "account/email/account_already_exists": Notice(
        _("Your Pyracast account already exists"),
        _("Someone tried to create a Pyracast account with %(email)s, which already has one."),
        security=False,
        action_key="password_reset_url",
        action_label=pgettext_lazy("account mail action", "Reset password"),
    ),
    "account/email/login_locked": Notice(
        _("Password sign-in is paused"),
        _(
            "After many failed sign-in attempts, signing in with your password is paused for up "
            "to 24 hours. Reset your password to sign in now."
        ),
        security=False,
        action_key="password_reset_url",
        action_label=pgettext_lazy("account mail action", "Reset password"),
    ),
}
_SENTENCE_KEYS = ("from_email", "to_email", "deleted_email", "provider", "email")
_BROWSERS = (
    ("EdgiOS/", "Edge"),
    ("EdgA/", "Edge"),
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("FxiOS/", "Firefox"),
    ("Firefox/", "Firefox"),
    ("CriOS/", "Chrome"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)
_SYSTEMS = (
    ("Windows", "Windows"),
    ("Android", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iOS"),
    ("Mac OS X", "macOS"),
    ("Linux", "Linux"),
)


def device_label(user_agent: str) -> str:
    browser = next((name for marker, name in _BROWSERS if marker in user_agent), "")
    system = next((name for marker, name in _SYSTEMS if marker in user_agent), "")
    if browser and system:
        return gettext("%(browser)s on %(system)s") % {"browser": browser, "system": system}
    return browser or system or gettext("an unknown device")


def mail_time(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    return f"{moment_text(local)} ({utc_offset(local)})"


def render_notice(notice: Notice, context: Mapping[str, Any]) -> tuple[str, str, str]:
    values = {key: str(context.get(key, "")) for key in _SENTENCE_KEYS}
    subject = str(notice.subject) % values
    timestamp = context.get("timestamp")
    template_context = {
        "title": subject,
        "sentence": str(notice.sentence) % values if notice.sentence else "",
        "security": notice.security,
        "time": mail_time(timestamp, ACCOUNT_ZONE) if timestamp else "",
        "device": device_label(str(context.get("user_agent", ""))),
        "ip": context.get("ip", ""),
        "secure_url": f"{settings.PYRACAST_PUBLIC_ORIGIN}/app/",
        "action_url": context.get(notice.action_key, ""),
        "action_label": notice.action_label,
    }
    return (
        subject,
        render_to_string("mail/notice.txt", template_context).strip(),
        render_to_string("mail/notice.html", template_context),
    )


def _address_owner(address: str) -> User | None:
    owner = (
        EmailAddress.objects.filter(email__iexact=address, verified=True)
        .select_related("user")
        .first()
    )
    return owner.user if owner is not None else None


@sensitive_variables("code", "context")
def compose_account_mail(
    mail: OutboundMail, code: str
) -> EmailMultiAlternatives | OutboundMailError:
    recipient = mail.recipient
    if mail.kind == OutboundMailKind.LOGIN_LOCKED:
        user = login_identity(mail.recipient)
        if user is None or not user.is_active:
            return OutboundMailError.NO_ACCOUNT
        recipient = EmailAddress.objects.get_primary_email(user) or user.email
    else:
        user = _address_owner(mail.recipient)
    context: dict[str, Any] = {
        **mail.context,
        "email": mail.recipient,
        "signup_url": settings.HEADLESS_FRONTEND_URLS["account_signup"],
        "password_reset_url": settings.HEADLESS_FRONTEND_URLS["account_reset_password"],
    }
    if "timestamp" in context:
        context["timestamp"] = datetime.fromisoformat(context["timestamp"])
    if code:
        context["code"] = code
    preference = user_language(user) if mail.owner_language and user is not None else None
    language = preference or mail.language
    with translation.override(language):
        return cast(
            EmailMultiAlternatives, get_adapter().render_mail(mail.kind, recipient, context)
        )
