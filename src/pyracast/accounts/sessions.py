import math
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from allauth.account import signals as account_signals
from allauth.mfa import signals as mfa_signals
from allauth.socialaccount import signals as social_signals
from allauth.socialaccount.models import SocialAccount
from allauth.usersessions.models import UserSession
from django.conf import settings
from django.contrib.auth import signals as auth_signals
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import User
from django.contrib.sessions.backends.base import SessionBase
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.models import Session
from django.db import transaction
from django.dispatch import Signal
from django.http import HttpRequest

from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.accounts.processes import record_authority

# Seconds between two writes of a session's activity marker and its device record.
ACTIVITY_REFRESH_SECONDS = 60
DEVICE_MARKER = "device_session_key"


class ExpiryReason(StrEnum):
    ABSOLUTE = "absolute"
    IDLE = "idle"
    INVALID_MARKER = "invalid_marker"


def start_session(*, request: HttpRequest, user: AbstractBaseUser, **_kwargs: Any) -> None:
    now = time.time()
    request.session["authenticated_at"] = now
    request.session["last_activity_at"] = now
    with transaction.atomic():
        record_authority(request, user)


def _marker_age(session: SessionBase, marker: str, now: float) -> float | None:
    value = session.get(marker)
    if not isinstance(value, int | float) or not math.isfinite(value) or value > now:
        return None
    return now - value


def expiry_reason(session: SessionBase, now: float) -> ExpiryReason | None:
    limits = (
        ("authenticated_at", settings.PYRACAST_SESSION_ABSOLUTE_SECONDS, ExpiryReason.ABSOLUTE),
        ("last_activity_at", settings.PYRACAST_SESSION_IDLE_SECONDS, ExpiryReason.IDLE),
    )
    for marker, limit, reason in limits:
        age = _marker_age(session, marker, now)
        if age is None:
            return ExpiryReason.INVALID_MARKER
        if age >= limit:
            return reason
    return None


def activity_refresh_due(session: SessionBase, now: float) -> bool:
    # A session without its device record, such as one signed in outside allauth, records it at
    # once: revocation finds sessions only through those records.
    return (
        session.get(DEVICE_MARKER) != session.session_key
        or now - float(session["last_activity_at"]) >= ACTIVITY_REFRESH_SECONDS
    )


def record_activity(request: HttpRequest, now: float) -> None:
    request.session["last_activity_at"] = now
    UserSession.objects.create_from_request(request)
    request.session[DEVICE_MARKER] = request.session.session_key


def _note_device(*, request: HttpRequest, **_kwargs: Any) -> None:
    request.session[DEVICE_MARKER] = request.session.session_key


def _hold_account(user_id: Any) -> None:
    # Revocation and key rotation of one account take this lock, so a session rotating while
    # another revokes is either found under its new key or finds itself ended.
    User.objects.select_for_update(no_key=True).filter(pk=user_id).values_list("pk").first()


@transaction.atomic
def end_sessions(
    user_id: Any, trigger: str, *, keep: str | None = None, request: HttpRequest | None = None
) -> int:
    _hold_account(user_id)
    keys = list(
        UserSession.objects.filter(user_id=user_id)
        .exclude(session_key=keep)
        .values_list("session_key", flat=True)
    )
    if not keys:
        return 0
    Session.objects.filter(session_key__in=keys).delete()
    UserSession.objects.filter(session_key__in=keys).delete()
    record_security_event(
        SecurityEvent.SESSIONS_REVOKED,
        request=request,
        user_id=user_id,
        reason=trigger,
        count=len(keys),
    )
    return len(keys)


def revoke_other_sessions(request: HttpRequest, user: AbstractBaseUser, trigger: str) -> None:
    end_sessions(user.pk, trigger, keep=request.session.session_key, request=request)


def _revoking(trigger: str) -> Callable[..., None]:
    def revoke(*, request: HttpRequest, user: AbstractBaseUser, **_kwargs: Any) -> None:
        revoke_other_sessions(request, user, trigger)

    return revoke


def _revoke_for_disconnect(
    *, request: HttpRequest, socialaccount: SocialAccount, **_kwargs: Any
) -> None:
    revoke_other_sessions(request, socialaccount.user, "social_account_removed")


def _forget_device(*, request: HttpRequest, **_kwargs: Any) -> None:
    UserSession.objects.filter(session_key=request.session.session_key).delete()


def _rotate_after_reauthentication(
    *, request: HttpRequest, user: AbstractBaseUser, reauthenticated: bool = False, **_kwargs: Any
) -> None:
    if not reauthenticated:
        return
    key = request.session.session_key
    with transaction.atomic():
        record_authority(request, user)
        _hold_account(user.pk)
        if not Session.objects.filter(session_key=key).exists():
            raise SessionInterrupted("Another session of the account ended this one.")
        UserSession.objects.filter(session_key=key).delete()
        request.session.cycle_key()
        record_activity(request, time.time())


# allauth signals an authenticator activation and a recovery-code replacement only after their
# commit; those writes end the other sessions in their own transaction (mfa_views).
REVOKING_SIGNALS: dict[Signal, str] = {
    mfa_signals.authenticator_removed: "mfa_removed",
    account_signals.email_changed: "email_changed",
    account_signals.password_changed: "password_changed",
    account_signals.password_set: "password_set",
    account_signals.password_reset: "password_reset",
}


def connect_session_lifecycle() -> None:
    auth_signals.user_logged_in.connect(start_session, dispatch_uid="pyracast.start_session")
    # allauth.usersessions stores the device record from this same signal.
    account_signals.user_logged_in.connect(_note_device, dispatch_uid="pyracast.note_device")
    auth_signals.user_logged_out.connect(_forget_device, dispatch_uid="pyracast.forget_device")
    account_signals.authentication_step_completed.connect(
        _rotate_after_reauthentication, dispatch_uid="pyracast.rotate_after_reauthentication"
    )
    social_signals.social_account_removed.connect(
        _revoke_for_disconnect, dispatch_uid="pyracast.revoke_for_disconnect"
    )
    for signal, trigger in REVOKING_SIGNALS.items():
        signal.connect(_revoking(trigger), weak=False, dispatch_uid=f"pyracast.revoke.{trigger}")
