import math
import time
from collections.abc import Callable
from typing import cast

from django.conf import settings
from django.contrib.auth import logout
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse

from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.accounts.authorization import requires_admin_mfa


class PrivilegedAuthenticatorEnrollment:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = request.user
        if (
            request.path.rstrip("/") == "/api/auth/browser/v1/account/authenticators/totp"
            and request.method in {"GET", "POST"}
            and user.is_authenticated
            and requires_admin_mfa(user)
        ):
            raise PermissionDenied(
                "Administrator authenticators require trusted operator provisioning."
            )
        return self.get_response(request)


class SessionLifetime:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            now = time.time()
            last_activity_at = request.session.get("last_activity_at")
            expiry = self._expiry(
                request.session.get("authenticated_at"),
                now,
                settings.SESSION_COOKIE_AGE,
                "absolute",
            ) or self._expiry(last_activity_at, now, settings.PYRACAST_SESSION_IDLE_SECONDS, "idle")
            if expiry is not None:
                record_security_event(
                    SecurityEvent.SESSION_EXPIRED,
                    request=request,
                    user_id=request.user.pk,
                    reason=expiry,
                )
                logout(request)
            elif now - cast(float, last_activity_at) >= 60:
                request.session["last_activity_at"] = now
        return self.get_response(request)

    @staticmethod
    def _expiry(timestamp: object, now: float, lifetime: int, reason: str) -> str | None:
        if (
            not isinstance(timestamp, int | float)
            or not math.isfinite(timestamp)
            or timestamp > now
        ):
            return "invalid_marker"
        return reason if now - timestamp >= lifetime else None
