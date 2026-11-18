from __future__ import annotations

import logging
from threading import Lock
from typing import TYPE_CHECKING, cast

from django.conf import settings
from django.core.cache import caches
from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle

from pyracast.api.permissions import route_policy

if TYPE_CHECKING:
    from rest_framework.views import APIView

logger = logging.getLogger("pyracast.api")
_budget_lock = Lock()


class RouteThrottle(SimpleRateThrottle):
    """Counts each signed-in user's requests per throttle scope of the route policy table."""

    def __init__(self) -> None:
        # The scope and its rate come from the route of each request, not from the class.
        pass

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
        logger.warning("api_throttled scope=%s user=%s", self.scope, request.user.pk)
        return False

    def get_cache_key(self, request: Request, view: APIView) -> str:
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}
