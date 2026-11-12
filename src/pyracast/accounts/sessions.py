from typing import Any

from allauth.account import signals as account_signals
from allauth.mfa import signals as mfa_signals
from allauth.usersessions.models import UserSession
from django.contrib.auth.base_user import AbstractBaseUser
from django.http import HttpRequest


def revoke_other_sessions(*, request: HttpRequest, user: AbstractBaseUser, **kwargs: Any) -> None:
    for session in UserSession.objects.filter(user=user).exclude(
        session_key=request.session.session_key
    ):
        session.end()


def connect_session_revocation() -> None:
    for signal in (
        mfa_signals.authenticator_added,
        mfa_signals.authenticator_removed,
        mfa_signals.authenticator_reset,
        account_signals.email_changed,
    ):
        signal.connect(revoke_other_sessions, dispatch_uid="pyracast.revoke_other_sessions")
