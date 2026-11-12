import json
import logging
from contextlib import suppress
from functools import partial
from ipaddress import ip_address
from typing import Any

from allauth.account import signals as account_signals
from allauth.account.adapter import get_adapter
from allauth.account.models import EmailAddress
from allauth.mfa import signals as mfa_signals
from django.contrib.auth import signals as auth_signals
from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest

logger = logging.getLogger("pyracast.security")


def record_security_event(
    event: str,
    *,
    request: HttpRequest | None = None,
    user: AbstractBaseUser | None = None,
    email_address: EmailAddress | None = None,
    **kwargs: Any,
) -> None:
    user_id = user.pk if user is not None else None
    if user_id is None and email_address is not None:
        user_id = email_address.user_id
    address = None
    if request is not None:
        with suppress(ValueError, PermissionDenied):
            address = str(ip_address(get_adapter().get_client_ip(request)))
    logger.info(
        "%s",
        json.dumps({"event": event, "user_id": user_id, "client_ip": address}, sort_keys=True),
    )


def connect_security_events() -> None:
    events = (
        (auth_signals.user_logged_in, "login_succeeded"),
        (auth_signals.user_logged_out, "logout"),
        (auth_signals.user_login_failed, "login_failed"),
        (account_signals.user_signed_up, "signup"),
        (account_signals.email_confirmed, "email_verified"),
        (account_signals.email_added, "email_added"),
        (account_signals.email_changed, "email_changed"),
        (account_signals.email_removed, "email_removed"),
        (account_signals.password_changed, "password_changed"),
        (account_signals.password_set, "password_set"),
        (account_signals.password_reset, "password_reset"),
        (mfa_signals.authenticator_added, "mfa_added"),
        (mfa_signals.authenticator_removed, "mfa_removed"),
        (mfa_signals.authenticator_reset, "mfa_reset"),
        (mfa_signals.authenticator_used, "mfa_succeeded"),
        (mfa_signals.authentication_failed, "mfa_failed"),
    )
    for signal, event in events:
        signal.connect(
            partial(record_security_event, event),
            weak=False,
            dispatch_uid=f"pyracast.security.{event}",
        )
