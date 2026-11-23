from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, cast

from allauth.account.adapter import get_adapter
from django.conf import settings
from django.core.cache import caches
from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle

from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.api.permissions import route_policy

if TYPE_CHECKING:
    from rest_framework.views import APIView

_budget_lock = Lock()


class RouteThrottle(SimpleRateThrottle):
    def get_rate(self) -> str | None:
        # The scope and its rate come from the route of each request, not from the class.
        return None

    def allow_request(self, request: Request, view: APIView) -> bool:
        policy = route_policy(request)
        if policy is None or policy.throttle is None:
            return True
        self.scope = policy.throttle.value
        rates = cast(dict[str, str], settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])
        self.rate = rates[self.scope]
        self.num_requests, self.duration = self.parse_rate(self.rate)
        self.cache = caches["throttle"]
        with _budget_lock:
            if super().allow_request(request, view):
                return True
        record_security_event(
            SecurityEvent.API_REQUEST_THROTTLED,
            request=request._request,
            user_id=request.user.pk,
            scope=self.scope,
        )
        return False

    def get_cache_key(self, request: Request, view: APIView) -> str:
        ident = (
            request.user.pk
            if request.user.is_authenticated
            else get_adapter().get_client_ip(request._request)
        )
        return self.cache_format % {"scope": self.scope, "ident": ident}
