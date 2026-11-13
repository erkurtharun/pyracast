from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from django.utils.cache import patch_cache_control

from pyracast.accounts.audit import record_security_event


class PrivateAPIResponses:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if request.path.startswith("/api/"):
            patch_cache_control(response, private=True, no_store=True)
        if request.path.startswith("/api/auth/") and response.status_code == 429:
            record_security_event("authentication_request_throttled", request=request)
        return response
