from collections.abc import Callable
from http.cookies import Morsel
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

    def stored_key(self) -> str | None:
        # The key of the stored session the request carried, read now if it was not yet; None
        # when it named none.
        self.keys()
        return self.session_key if self.found else None


def lapse(request: HttpRequest) -> None:
    cast(SessionStore, request.session).lapsed = True


def persist(request: HttpRequest) -> None:
    # Saves a changed session in the open transaction, a new one included, so its data commits
    # with the account's. A session ended meanwhile is answered as ended, any other failure of
    # the save as itself.
    if not request.session.modified:
        return
    try:
        request.session.save()
    except UpdateError as error:
        failure = error.__context__
        if isinstance(failure, DatabaseError) and not isinstance(failure, ObjectNotUpdated):
            raise failure from failure.__cause__
        raise SessionInterrupted("Another session of the account ended this one.") from None


def _ended(cookie: Morsel[str]) -> bool:
    age = cookie["max-age"]
    return isinstance(age, int) and age <= 0


class SessionCookie(SessionMiddleware):
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        super().__init__(get_response)
        if not issubclass(self.SessionStore, SessionStore):
            raise ImproperlyConfigured(f"SESSION_ENGINE must be {__name__}.")

    def process_response(self, request: HttpRequest, response: HttpResponse) -> HttpResponse:
        response = super().process_response(request, response)
        name = settings.SESSION_COOKIE_NAME
        cookie = response.cookies.get(name)
        if cookie is None:
            return response
        # A browser deletes a cookie without lifetime left; sent late, it would end a newer sign-in.
        if cookie.value and _ended(cookie):
            del response.cookies[name]
            return response
        carried = request.COOKIES.get(name)
        if carried is None:
            return response
        if cookie.value:
            keep = cookie.value != carried
        else:
            session = cast(SessionStore, request.session)
            keep = session.found and not session.lapsed
        if not keep:
            del response.cookies[name]
        return response
