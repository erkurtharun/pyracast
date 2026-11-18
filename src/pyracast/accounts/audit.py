import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import suppress
from enum import StrEnum
from functools import partial
from ipaddress import ip_address
from typing import Any

from allauth.account import signals as account_signals
from allauth.account.adapter import get_adapter
from allauth.mfa import signals as mfa_signals
from allauth.socialaccount import signals as social_signals
from django.contrib.auth import signals as auth_signals
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.dispatch import Signal
from django.http import HttpRequest
from django.utils.crypto import salted_hmac

logger = logging.getLogger("pyracast.security")


class SecurityEvent(StrEnum):
    LOGIN_SUCCEEDED = "login_succeeded"
    LOGOUT = "logout"
    LOGIN_FAILED = "login_failed"
    LOGIN_LOCKED = "login_locked"
    LOGIN_CODE_REJECTED = "login_code_rejected"
    SIGNUP = "signup"
    EMAIL_VERIFIED = "email_verified"
    EMAIL_CODE_REJECTED = "email_code_rejected"
    EMAIL_ADDED = "email_added"
    EMAIL_CHANGED = "email_changed"
    EMAIL_REMOVED = "email_removed"
    CREDENTIAL_CHANGED = "password_changed"
    CREDENTIAL_SET = "password_set"
    CREDENTIAL_RESET = "password_reset"
    RESET_CODE_REJECTED = "password_reset_code_rejected"
    CURRENT_CREDENTIAL_REFUSED = "password_change_failed"
    MFA_ADDED = "mfa_added"
    MFA_REMOVED = "mfa_removed"
    MFA_RESET = "mfa_reset"
    MFA_SUCCEEDED = "mfa_succeeded"
    MFA_FAILED = "mfa_failed"
    SECOND_FACTOR_LOCKED = "second_factor_locked"
    SOCIAL_ACCOUNT_CONNECTED = "social_account_connected"
    SOCIAL_ACCOUNT_DISCONNECTED = "social_account_disconnected"
    ADMIN_MFA_PROVISIONED = "admin_mfa_provisioned"
    INVALID_PROXY_HEADERS = "invalid_proxy_headers"
    AUTHENTICATION_REQUEST_THROTTLED = "authentication_request_throttled"
    AUTHORIZATION_DENIED = "authorization_denied"
    CSRF_REFUSED = "csrf_refused"
    INVITATION_SENT = "invitation_sent"
    INVITATION_LIMITED = "invitation_limited"
    INVITATION_REVOKED = "invitation_revoked"
    INVITATION_ACCEPTED = "invitation_accepted"
    WORKSPACE_MEMBER_REMOVED = "workspace_member_removed"
    WORKSPACE_ADMIN_REASSIGNED = "workspace_admin_reassigned"


SIGNAL_EVENTS: Mapping[Signal, SecurityEvent] = {
    auth_signals.user_logged_in: SecurityEvent.LOGIN_SUCCEEDED,
    auth_signals.user_logged_out: SecurityEvent.LOGOUT,
    auth_signals.user_login_failed: SecurityEvent.LOGIN_FAILED,
    account_signals.user_signed_up: SecurityEvent.SIGNUP,
    account_signals.email_confirmed: SecurityEvent.EMAIL_VERIFIED,
    account_signals.email_added: SecurityEvent.EMAIL_ADDED,
    account_signals.email_changed: SecurityEvent.EMAIL_CHANGED,
    account_signals.email_removed: SecurityEvent.EMAIL_REMOVED,
    account_signals.password_changed: SecurityEvent.CREDENTIAL_CHANGED,
    account_signals.password_set: SecurityEvent.CREDENTIAL_SET,
    account_signals.password_reset: SecurityEvent.CREDENTIAL_RESET,
    account_signals.login_code_rejected: SecurityEvent.LOGIN_CODE_REJECTED,
    account_signals.password_reset_code_rejected: SecurityEvent.RESET_CODE_REJECTED,
    account_signals.email_verification_code_rejected: SecurityEvent.EMAIL_CODE_REJECTED,
    mfa_signals.authenticator_added: SecurityEvent.MFA_ADDED,
    mfa_signals.authenticator_removed: SecurityEvent.MFA_REMOVED,
    mfa_signals.authenticator_reset: SecurityEvent.MFA_RESET,
    mfa_signals.authenticator_used: SecurityEvent.MFA_SUCCEEDED,
    mfa_signals.authentication_failed: SecurityEvent.MFA_FAILED,
    social_signals.social_account_added: SecurityEvent.SOCIAL_ACCOUNT_CONNECTED,
    social_signals.social_account_removed: SecurityEvent.SOCIAL_ACCOUNT_DISCONNECTED,
}

# A line never carries an address, name, token, code or password: an address becomes `subject`.
LINE_KEYS = frozenset(
    {"event", "user_id", "client_ip", "subject", "tenant_id", "target_id", "reason", "view"}
)


def redacted_key(address: str) -> str:
    return salted_hmac(
        "pyracast.security.subject", address.strip().lower(), algorithm="sha256"
    ).hexdigest()[:16]


def _client_ip(request: HttpRequest | None, client_ip: str | None) -> str | None:
    if request is not None:
        with suppress(ValueError, PermissionDenied):
            return str(ip_address(get_adapter().get_client_ip(request)))
    return client_ip


def record_security_event(
    event: SecurityEvent,
    *,
    request: HttpRequest | None = None,
    user_id: int | None = None,
    client_ip: str | None = None,
    address: str | None = None,
    tenant_id: int | None = None,
    target_id: int | None = None,
    reason: str | None = None,
    view: str | None = None,
) -> None:
    details = {
        "subject": None if address is None else redacted_key(address),
        "tenant_id": tenant_id,
        "target_id": target_id,
        "reason": reason,
        "view": view,
    }
    line = {"event": event.value, "user_id": user_id, "client_ip": _client_ip(request, client_ip)}
    line |= {key: value for key, value in details.items() if value is not None}
    logger.info("%s", json.dumps(line, sort_keys=True))


def record_on_commit(event: SecurityEvent, **fields: Any) -> None:
    transaction.on_commit(partial(record_security_event, event, **fields))


class RefusalThrottle:
    # Seconds between two lines for one actor, view and refusal code, per process.
    interval = 60.0
    capacity = 4096

    def __init__(self) -> None:
        self._seen: OrderedDict[tuple[int, str, str], float] = OrderedDict()
        self._lock = threading.Lock()

    def admit(self, key: tuple[int, str, str]) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._seen.get(key)
            if last is not None and now - last < self.interval:
                return False
            self._seen[key] = now
            self._seen.move_to_end(key)
            while len(self._seen) > self.capacity:
                self._seen.popitem(last=False)
            return True


refusal_throttle = RefusalThrottle()


def record_refusal(
    event: SecurityEvent,
    *,
    request: HttpRequest,
    user_id: int,
    view: str,
    reason: str,
    tenant_id: int | None,
) -> None:
    if refusal_throttle.admit((user_id, view, reason)):
        record_security_event(
            event, request=request, user_id=user_id, tenant_id=tenant_id, reason=reason, view=view
        )


def _signal_user_id(arguments: Mapping[str, Any]) -> int | None:
    if (user := arguments.get("user")) is not None:
        return int(user.pk)
    for name in ("email_address", "socialaccount"):
        if (record := arguments.get(name)) is not None:
            return int(record.user_id)
    if (login := arguments.get("sociallogin")) is not None:
        return int(login.account.user_id)
    return None


def _signal_address(arguments: Mapping[str, Any]) -> str | None:
    credentials = arguments.get("credentials") or {}
    identifier = credentials.get("email") or credentials.get("username")
    return identifier if isinstance(identifier, str) else None


def _receive(event: SecurityEvent, *, request: HttpRequest | None = None, **arguments: Any) -> None:
    record_security_event(
        event,
        request=request,
        user_id=_signal_user_id(arguments),
        address=_signal_address(arguments),
    )


def connect_security_events() -> None:
    for signal, event in SIGNAL_EVENTS.items():
        signal.connect(
            partial(_receive, event), weak=False, dispatch_uid=f"pyracast.security.{event}"
        )
