from collections.abc import Callable
from typing import Any, cast

from django.conf import settings
from django.contrib.sessions.backends import db
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse


class SessionStore(db.SessionStore):
    # Whether the key the request carried named a stored session when it was read.
    found = False

    def load(self) -> dict[str, Any]:
        data = super().load()
        self.found = self.session_key is not None
        return data


class SessionCookie(SessionMiddleware):
    # A response sends the session cookie only when its request created or rotated the key, and
    # deletes it only when its request ended the stored session the cookie named, so an answer
    # that reaches the browser after a newer one never replaces or deletes the newer key.
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        super().__init__(get_response)
        if not issubclass(self.SessionStore, SessionStore):
            raise ImproperlyConfigured(f"SESSION_ENGINE must be {__name__}.")

    def process_response(self, request: HttpRequest, response: HttpResponse) -> HttpResponse:
        response = super().process_response(request, response)
        name = settings.SESSION_COOKIE_NAME
        carried = request.COOKIES.get(name)
        cookie = response.cookies.get(name)
        if carried is None or cookie is None:
            return response
        if cookie["max-age"] == 0:
            keep = cast(SessionStore, request.session).found
        else:
            keep = cookie.value != carried
        if not keep:
            del response.cookies[name]
        return response
