import time
from collections.abc import Callable

from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponse

from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.accounts.session_cookie import lapse
from pyracast.accounts.sessions import activity_refresh_due, expiry_reason, record_activity


class SessionLifetime:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            now = time.time()
            if (reason := expiry_reason(request.session, now)) is not None:
                record_security_event(
                    SecurityEvent.SESSION_EXPIRED,
                    request=request,
                    user_id=request.user.pk,
                    reason=reason,
                )
                lapse(request)
                logout(request)
            elif activity_refresh_due(request.session, now):
                record_activity(request, now)
        return self.get_response(request)
