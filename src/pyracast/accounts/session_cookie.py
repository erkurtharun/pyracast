from collections.abc import Callable
from typing import Any, cast

from django.conf import settings
from django.contrib.sessions.backends import db
from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ImproperlyConfigured, ObjectNotUpdated
from django.db import DatabaseError
from django.http import HttpRequest, HttpResponse


class SessionStore(db.SessionStore):
    # Whether the key the request carried named a stored session when it was read.
    found = False
    # Whether the request ended the session because it expired.
    lapsed = False

    def load(self) -> dict[str, Any]:
        data = super().load()
        self.found = self.session_key is not None
        return data


def lapse(request: HttpRequest) -> None:
    cast(SessionStore, request.session).lapsed = True


def persist(request: HttpRequest) -> None:
    # Saves a stored session in the open transaction, so its data commits with the account's. A
    # session ended meanwhile is answered as ended, any other failure of the save as itself.
    if request.session.session_key is None:
        return
    try:
        request.session.save()
    except UpdateError as error:
        failure = error.__context__
        if isinstance(failure, DatabaseError) and not isinstance(failure, ObjectNotUpdated):
            raise failure from failure.__cause__
        raise SessionInterrupted("Another session of the account ended this one.") from None


class SessionCookie(SessionMiddleware):
    # A response sends the session cookie only when its request created or rotated the key, and
    # deletes it only when its request ended the stored session the cookie named other than by
    # expiry; an expired session leaves its key, which names no session. So an answer reaching
    # the browser late never restores an older key, and an expiry never deletes the key of a
    # later sign-in.
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
            session = cast(SessionStore, request.session)
            keep = session.found and not session.lapsed
        else:
            keep = cookie.value != carried
        if not keep:
            del response.cookies[name]
        return response
