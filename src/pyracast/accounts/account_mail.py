from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.template.loader import render_to_string
from django.utils.functional import Promise
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True)
class Notice:
    subject: Promise
    sentence: Promise
    security: bool = True
    action_key: str = ""
    action_label: Promise | None = None


NOTICES = {
    "account/email/password_changed": Notice(
        _("Your password was changed"), _("The password of your Pyracast account was changed.")
    ),
    "account/email/password_set": Notice(
        _("A password was set for your account"),
        _("A password was set for your Pyracast account."),
    ),
    "account/email/password_reset": Notice(
        _("Your password was reset"), _("The password of your Pyracast account was reset.")
    ),
    "account/email/email_changed": Notice(
        _("Your email address was changed"),
        _("Your Pyracast email address changed from %(from_email)s to %(to_email)s."),
    ),
    "account/email/email_confirm": Notice(
        _("Your email address was verified"),
        _("The email address of your Pyracast account was verified."),
    ),
    "account/email/email_deleted": Notice(
        _("An email address was removed"),
        _("%(deleted_email)s was removed from your Pyracast account."),
    ),
    "mfa/email/totp_activated": Notice(
        _("Two-factor authentication was turned on"),
        _("Two-factor authentication with an authenticator app was turned on for your account."),
    ),
    "mfa/email/totp_deactivated": Notice(
        _("Two-factor authentication was turned off"),
        _("Two-factor authentication with an authenticator app was turned off for your account."),
    ),
    "account/email/second_factor_reset": Notice(
        _("Your second factor was reset"),
        _(
            "An operator removed the second factors of your Pyracast account and "
            "ended its sessions."
        ),
        security=False,
    ),
    "mfa/email/recovery_codes_generated": Notice(
        _("New recovery codes were generated"),
        _("New recovery codes were generated for your account. The previous codes no longer work."),
    ),
    "socialaccount/email/account_connected": Notice(
        _("A sign-in provider was connected"),
        _("%(provider)s was connected to your Pyracast account for signing in."),
    ),
    "socialaccount/email/account_disconnected": Notice(
        _("A sign-in provider was disconnected"),
        _("%(provider)s was disconnected from your Pyracast account."),
    ),
    "account/email/unknown_account": Notice(
        _("Pyracast account not found"),
        _("Someone tried to use %(email)s with Pyracast, but no account uses this address."),
        security=False,
        action_key="signup_url",
        action_label=_("Create an account"),
    ),
    "account/email/account_already_exists": Notice(
        _("Your Pyracast account already exists"),
        _("Someone tried to create a Pyracast account with %(email)s, which already has one."),
        security=False,
        action_key="password_reset_url",
        action_label=_("Reset password"),
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


def render_notice(notice: Notice, context: Mapping[str, Any]) -> tuple[str, str, str]:
    values = {key: str(context.get(key, "")) for key in _SENTENCE_KEYS}
    template_context = {
        "title": str(notice.subject),
        "sentence": str(notice.sentence) % values,
        "security": notice.security,
        "time": context.get("timestamp"),
        "device": device_label(str(context.get("user_agent", ""))),
        "ip": context.get("ip", ""),
        "secure_url": f"{settings.PYRACAST_PUBLIC_ORIGIN}/app/",
        "action_url": context.get(notice.action_key, ""),
        "action_label": notice.action_label,
    }
    return (
        str(notice.subject),
        render_to_string("mail/notice.txt", template_context).strip(),
        render_to_string("mail/notice.html", template_context),
    )
