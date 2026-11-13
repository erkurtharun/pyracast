from email.utils import parseaddr
from typing import Any, cast
from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import CheckMessage, Error, Tags, register
from django.core.exceptions import ImproperlyConfigured


def validate_public_origin() -> None:
    try:
        origin = urlsplit(settings.PYRACAST_PUBLIC_ORIGIN)
        _ = origin.port
    except ValueError:
        raise ImproperlyConfigured(
            "PYRACAST_PUBLIC_ORIGIN must be a valid public origin."
        ) from None
    if (
        origin.scheme not in ({"http", "https"} if settings.DEBUG else {"https"})
        or not origin.hostname
        or origin.username is not None
        or origin.password is not None
        or origin.path
        or origin.query
        or origin.fragment
    ):
        raise ImproperlyConfigured("PYRACAST_PUBLIC_ORIGIN must be a valid public origin.")


@register(Tags.security, deploy=True)
def check_authentication_origin(**_kwargs: Any) -> list[CheckMessage]:
    try:
        validate_public_origin()
    except ImproperlyConfigured as error:
        return [Error(str(error), id="accounts.E002")]
    return []


@register(Tags.security, deploy=True)
def check_authentication_mail_delivery(**_kwargs: Any) -> list[CheckMessage]:
    mailer = cast(dict[str, Any], settings.MAILERS["default"])
    address = parseaddr(settings.DEFAULT_FROM_EMAIL)[1]
    if not settings.DEBUG and (
        mailer["BACKEND"] != "django.core.mail.backends.smtp.EmailBackend"
        or not mailer["OPTIONS"].get("host")
        or "@" not in address
        or address.rsplit("@", 1)[-1] == "localhost"
    ):
        return [
            Error(
                "Verified signup and account recovery require production SMTP and a public sender.",
                hint="Configure PYRACAST_EMAIL_HOST and PYRACAST_FROM_EMAIL, then verify delivery.",
                id="accounts.E001",
            )
        ]
    return []
