from email.utils import parseaddr
from ipaddress import ip_address
from socket import inet_aton
from typing import Any, cast
from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import CheckMessage, Error, Tags, register
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import validate_email

from pyracast.mail.errors import NotSentError
from pyracast.mail.send import configured_mailer

SMTP_BACKEND = "pyracast.mail.smtp.EmailBackend"
PRODUCTION_BACKENDS = {SMTP_BACKEND, "pyracast.mail.gmail.EmailBackend"}


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


# Each entry reads what the one before it established: the session lock, the session, the user,
# the session's limits, then the pending account processes resting on that account.
SESSION_MIDDLEWARE = (
    "pyracast.accounts.session_lock.SessionRequestSerialization",
    "pyracast.accounts.session_cookie.SessionCookie",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "pyracast.accounts.middleware.SessionLifetime",
    "pyracast.accounts.processes.PendingProcessAuthority",
)


@register(Tags.security)
def check_session_middleware_order(**_kwargs: Any) -> list[CheckMessage]:
    middleware = list(settings.MIDDLEWARE)
    positions = [middleware.index(name) for name in SESSION_MIDDLEWARE if name in middleware]
    if len(positions) == len(SESSION_MIDDLEWARE) and positions == sorted(positions):
        return []
    return [
        Error(
            "The session middleware is missing or out of order.",
            hint="List in this order: " + ", ".join(SESSION_MIDDLEWARE) + ".",
            id="accounts.E003",
        )
    ]


@register(Tags.security, deploy=True)
def check_authentication_origin(**_kwargs: Any) -> list[CheckMessage]:
    try:
        validate_public_origin()
    except ImproperlyConfigured as error:
        return [Error(str(error), id="accounts.E002")]
    return []


def _mail_problem() -> str | None:
    mailer = cast(dict[str, Any], settings.MAILERS["default"])
    options = cast(dict[str, Any], mailer.get("OPTIONS", {}))
    address = parseaddr(settings.DEFAULT_FROM_EMAIL)[1]
    if mailer["BACKEND"] not in PRODUCTION_BACKENDS:
        return "Configure the SMTP or Gmail mail backend."
    if not _public_mailbox(address):
        return "Set PYRACAST_FROM_EMAIL to a public sender address."
    if mailer["BACKEND"] == SMTP_BACKEND:
        if _loopback(str(options.get("host", ""))):
            return "Set PYRACAST_EMAIL_HOST to the public mail relay."
        if not 1 <= int(options.get("port", 25)) <= 65535:
            return "Set PYRACAST_EMAIL_PORT to a TCP port from 1 to 65535."
        if not options.get("use_tls") and not options.get("use_ssl"):
            return "Enable PYRACAST_EMAIL_TLS or PYRACAST_EMAIL_SSL."
    try:
        configured_mailer()
    except NotSentError:
        return "The mail backend rejects its configuration."
    return None


def _public_mailbox(address: str) -> bool:
    try:
        validate_email(address)
    except ValidationError:
        return False
    return address.rsplit("@", 1)[-1].lower() != "localhost"


def _loopback(host: str) -> bool:
    if not host or host.lower() == "localhost":
        return True
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        address = ip_address(host)
    except ValueError:
        try:
            address = ip_address(inet_aton(host))
        except OSError:
            return False
    return address.is_loopback or address.is_unspecified


@register(Tags.security, deploy=True)
def check_authentication_mail_delivery(**_kwargs: Any) -> list[CheckMessage]:
    if settings.DEBUG or (problem := _mail_problem()) is None:
        return []
    return [
        Error(
            "Verified signup and account recovery require production mail and a public sender.",
            hint=f"{problem} Then run pyracast check_mail.",
            id="accounts.E001",
        )
    ]
