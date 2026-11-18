import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from enum import StrEnum
from functools import partial
from ipaddress import ip_address
from typing import Any

from allauth.account import signals as account_signals
from allauth.account.adapter import get_adapter
from allauth.account.internal.flows.login import get_authentication_records
from allauth.mfa import signals as mfa_signals
from allauth.socialaccount import signals as social_signals
from django.contrib.auth import signals as auth_signals
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.dispatch import Signal
from django.http import HttpRequest
from django.utils.crypto import salted_hmac

from pyracast.event_details import INTEGER, OPTIONAL_INTEGER, OPTIONAL_TEXT, TEXT, Kind

logger = logging.getLogger("pyracast.security")
failure_logger = logging.getLogger(__name__)


class SecurityEvent(StrEnum):
    LOGIN_SUCCEEDED = "login_succeeded"
    LOGOUT = "logout"
    LOGIN_FAILED = "login_failed"
    LOGIN_REFUSED = "login_refused"
    LOGIN_LOCKED = "login_locked"
    LOGIN_CODE_REJECTED = "login_code_rejected"
    SESSION_EXPIRED = "session_expired"
    REAUTHENTICATED = "reauthenticated"
    REAUTHENTICATION_FAILED = "reauthentication_failed"
    SIGNUP = "signup"
    EMAIL_VERIFIED = "email_verified"
    EMAIL_CODE_REJECTED = "email_code_rejected"
    EMAIL_ADDED = "email_added"
    EMAIL_CHANGE_REQUESTED = "email_change_requested"
    EMAIL_CHANGED = "email_changed"
    EMAIL_REMOVED = "email_removed"
    CREDENTIAL_CHANGED = "password_changed"
    CREDENTIAL_SET = "password_set"
    RESET_REQUESTED = "password_reset_requested"
    CREDENTIAL_RESET = "password_reset"
    RESET_CODE_REJECTED = "password_reset_code_rejected"
    CURRENT_CREDENTIAL_REFUSED = "password_change_failed"
    MFA_ADDED = "mfa_added"
    MFA_REMOVED = "mfa_removed"
    MFA_RESET = "mfa_reset"
    MFA_SUCCEEDED = "mfa_succeeded"
    MFA_FAILED = "mfa_failed"
    AUTHENTICATOR_UNREADABLE = "mfa_secret_unreadable"
    SECOND_FACTOR_LOCKED = "second_factor_locked"
    SOCIAL_LOGIN_REFUSED = "social_login_refused"
    SOCIAL_ACCOUNT_CONNECTED = "social_account_connected"
    SOCIAL_ACCOUNT_DISCONNECTED = "social_account_disconnected"
    ADMIN_MFA_PROVISIONED = "admin_mfa_provisioned"
    INVALID_PROXY_HEADERS = "invalid_proxy_headers"
    AUTHENTICATION_REQUEST_THROTTLED = "authentication_request_throttled"
    API_REQUEST_THROTTLED = "api_throttled"
    AUTHORIZATION_DENIED = "authorization_denied"
    CSRF_REFUSED = "csrf_refused"
    INVITATION_SENT = "invitation_sent"
    INVITATION_LIMITED = "invitation_limited"
    INVITATION_REVOKED = "invitation_revoked"
    INVITATION_ACCEPTED = "invitation_accepted"
    INVITATION_DENIED = "invitation_denied"
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
    account_signals.authentication_step_completed: SecurityEvent.REAUTHENTICATED,
    account_signals.email_confirmation_sent: SecurityEvent.EMAIL_CHANGE_REQUESTED,
    mfa_signals.authenticator_added: SecurityEvent.MFA_ADDED,
    mfa_signals.authenticator_removed: SecurityEvent.MFA_REMOVED,
    mfa_signals.authenticator_reset: SecurityEvent.MFA_RESET,
    mfa_signals.authenticator_used: SecurityEvent.MFA_SUCCEEDED,
    mfa_signals.authentication_failed: SecurityEvent.MFA_FAILED,
    social_signals.social_account_added: SecurityEvent.SOCIAL_ACCOUNT_CONNECTED,
    social_signals.social_account_removed: SecurityEvent.SOCIAL_ACCOUNT_DISCONNECTED,
}

# A line never carries an address, name, token, code or password: an address becomes `subject`.
# `method` is allauth's step (password, code, mfa, socialaccount); `provider` a provider id.
LINE_KINDS: Mapping[str, Kind] = {
    "user_id": OPTIONAL_INTEGER,
    "client_ip": OPTIONAL_TEXT,
    "subject": TEXT,
    "tenant_id": INTEGER,
    "target_id": INTEGER,
    "reason": TEXT,
    "view": TEXT,
    "method": TEXT,
    "authenticator_type": TEXT,
    "provider": TEXT,
    "scope": TEXT,
}
LINE_KEYS = frozenset({"event", *LINE_KINDS})
# The details each event may carry beside event, user and client IP; a line with any other
# detail is refused. A signalled event carries what allauth reports about the step, and of
# those only a failed sign-in names an address.
SIGNALLED_DETAILS = frozenset({"method", "authenticator_type", "provider"})
WORKSPACE_DETAILS = frozenset({"tenant_id", "target_id"})
REFUSAL_DETAILS = frozenset({"tenant_id", "reason", "view"})
SECURITY_EVENT_DETAILS: Mapping[SecurityEvent, frozenset[str]] = {
    **dict.fromkeys(SIGNAL_EVENTS.values(), SIGNALLED_DETAILS),
    SecurityEvent.LOGIN_FAILED: SIGNALLED_DETAILS | {"subject"},
    SecurityEvent.REAUTHENTICATION_FAILED: SIGNALLED_DETAILS,
    SecurityEvent.LOGIN_REFUSED: frozenset({"reason"}),
    SecurityEvent.LOGIN_LOCKED: frozenset({"subject"}),
    SecurityEvent.SESSION_EXPIRED: frozenset({"reason"}),
    SecurityEvent.RESET_REQUESTED: frozenset({"subject"}),
    SecurityEvent.CURRENT_CREDENTIAL_REFUSED: frozenset(),
    SecurityEvent.AUTHENTICATOR_UNREADABLE: frozenset(),
    SecurityEvent.SECOND_FACTOR_LOCKED: frozenset(),
    SecurityEvent.SOCIAL_LOGIN_REFUSED: frozenset({"provider", "reason"}),
    SecurityEvent.ADMIN_MFA_PROVISIONED: frozenset(),
    SecurityEvent.INVALID_PROXY_HEADERS: frozenset(),
    SecurityEvent.AUTHENTICATION_REQUEST_THROTTLED: frozenset(),
    SecurityEvent.API_REQUEST_THROTTLED: frozenset({"scope"}),
    SecurityEvent.AUTHORIZATION_DENIED: REFUSAL_DETAILS,
    SecurityEvent.CSRF_REFUSED: REFUSAL_DETAILS,
    SecurityEvent.INVITATION_SENT: WORKSPACE_DETAILS | {"subject"},
    SecurityEvent.INVITATION_LIMITED: frozenset({"tenant_id", "subject", "reason"}),
    SecurityEvent.INVITATION_REVOKED: WORKSPACE_DETAILS,
    SecurityEvent.INVITATION_ACCEPTED: WORKSPACE_DETAILS,
    SecurityEvent.INVITATION_DENIED: WORKSPACE_DETAILS | {"reason"},
    SecurityEvent.WORKSPACE_MEMBER_REMOVED: WORKSPACE_DETAILS,
    SecurityEvent.WORKSPACE_ADMIN_REASSIGNED: WORKSPACE_DETAILS,
}


def redacted_key(address: str) -> str:
    return salted_hmac(
        "pyracast.security.subject", address.strip().lower(), algorithm="sha256"
    ).hexdigest()[:16]


def _client_ip(request: HttpRequest | None, client_ip: str | None) -> str | None:
    if request is not None:
        with suppress(ValueError, PermissionDenied):
            return str(ip_address(get_adapter().get_client_ip(request)))
    return client_ip


@contextmanager
def _contained(event: SecurityEvent) -> Iterator[None]:
    # A line that cannot be written must not fail the request it describes. Only the error type
    # is logged: an exception message can carry an address.
    try:
        yield
    except Exception as error:
        failure_logger.error(
            "security_line_failed event=%s error_type=%s", event, type(error).__name__
        )


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
    method: str | None = None,
    authenticator_type: str | None = None,
    provider: str | None = None,
    scope: str | None = None,
) -> None:
    with _contained(event):
        details = {
            "subject": None if address is None else redacted_key(address),
            "tenant_id": tenant_id,
            "target_id": target_id,
            "reason": reason,
            "view": view,
            "method": method,
            "authenticator_type": authenticator_type,
            "provider": provider,
            "scope": scope,
        }
        carried = {key: value for key, value in details.items() if value is not None}
        if undeclared := carried.keys() - SECURITY_EVENT_DETAILS[event]:
            raise ValueError(f"{event} declares no {', '.join(sorted(undeclared))}.")
        line = {
            "event": event.value,
            "user_id": user_id,
            "client_ip": _client_ip(request, client_ip),
        } | carried
        if mistyped := [
            key for key, kind in LINE_KINDS.items() if key in line and not kind.accepts(line[key])
        ]:
            raise ValueError(f"{event} takes {', '.join(mistyped)} of another type.")
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
    if (confirmation := arguments.get("confirmation")) is not None:
        # A code verification process names its user; a link confirmation names the address.
        owner = getattr(confirmation, "user", None)
        return int(owner.pk if owner is not None else confirmation.email_address.user_id)
    return None


def _signal_address(arguments: Mapping[str, Any]) -> str | None:
    credentials = arguments.get("credentials") or {}
    identifier = credentials.get("email") or credentials.get("username")
    return identifier if isinstance(identifier, str) else None


def _signal_provider(arguments: Mapping[str, Any]) -> str | None:
    if (account := arguments.get("socialaccount")) is not None:
        return str(account.provider)
    if (login := arguments.get("sociallogin")) is not None:
        return str(login.account.provider)
    return None


def _text(value: object) -> str | None:
    return str(value) if isinstance(value, str) else None


def _routed(
    event: SecurityEvent, request: HttpRequest | None, arguments: Mapping[str, Any]
) -> SecurityEvent | None:
    if event is SecurityEvent.REAUTHENTICATED and not arguments.get("reauthenticated"):
        return None
    if event is SecurityEvent.EMAIL_CHANGE_REQUESTED and arguments.get("signup"):
        return None
    if event is SecurityEvent.EMAIL_CHANGED and arguments.get("from_email_address") is None:
        # The first verification of a sign-up address is already `email_verified`.
        return None
    if event is SecurityEvent.MFA_FAILED and arguments.get("reauthentication"):
        return SecurityEvent.REAUTHENTICATION_FAILED
    # allauth sends `login_code_rejected` for sign-in codes and for reauthentication codes; only
    # the latter come from a signed-in session.
    user = getattr(request, "user", None)
    if event is SecurityEvent.LOGIN_CODE_REJECTED and user is not None and user.is_authenticated:
        return SecurityEvent.REAUTHENTICATION_FAILED
    return event


def _last_step(request: HttpRequest | None, user: Any) -> Mapping[str, Any]:
    if request is None or user is None or not hasattr(request, "session"):
        return {}
    records = get_authentication_records(request, user)
    return records[-1] if records else {}


def _write_signalled(
    signal_event: SecurityEvent, request: HttpRequest | None, arguments: Mapping[str, Any]
) -> None:
    event = _routed(signal_event, request, arguments)
    if event is None:
        return
    authenticator = arguments.get("authenticator")
    step = (
        _last_step(request, arguments.get("user"))
        if event is SecurityEvent.LOGIN_SUCCEEDED
        else arguments
    )
    method = _text(step.get("method"))
    if event is SecurityEvent.REAUTHENTICATION_FAILED:
        method = "mfa" if authenticator is not None else "code"
    record_security_event(
        event,
        request=request,
        user_id=_signal_user_id(arguments),
        address=_signal_address(arguments),
        method=method,
        authenticator_type=_text(step.get("type") if authenticator is None else authenticator.type),
        provider=_signal_provider(arguments) or _text(step.get("provider")),
    )


def _receive(
    signal_event: SecurityEvent, *, request: HttpRequest | None = None, **arguments: Any
) -> None:
    with _contained(signal_event):
        _write_signalled(signal_event, request, arguments)


def connect_security_events() -> None:
    for signal, event in SIGNAL_EVENTS.items():
        signal.connect(
            partial(_receive, event), weak=False, dispatch_uid=f"pyracast.security.{event}"
        )
