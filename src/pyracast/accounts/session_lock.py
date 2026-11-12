import hashlib
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from django.conf import settings
from django.contrib.sessions.exceptions import SessionInterrupted
from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError, connections, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse

_LOCK_TIMEOUT = "5s"


class SessionBusyError(Exception):
    pass


@contextmanager
def serialize_session(session_key: str) -> Iterator[None]:
    connection = connections["default"]
    if connection.settings_dict["OPTIONS"].get("pool") not in (None, False):
        raise ImproperlyConfigured("Session serialization requires a direct PostgreSQL connection.")
    digest = hashlib.sha256(b"pyracast.session-request.v1\0" + session_key.encode("ascii")).digest()
    lock_id = int.from_bytes(digest[:8], signed=True)
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('lock_timeout')")
            previous_timeout = cursor.fetchone()[0]
            cursor.execute("SELECT set_config('lock_timeout', %s, true)", [_LOCK_TIMEOUT])
            cursor.execute("SELECT pg_advisory_lock(%s)", [lock_id])
            locked_connection = connection.connection
            cursor.execute("SELECT set_config('lock_timeout', %s, true)", [previous_timeout])
    except BaseException as error:
        connection.close()
        if (
            isinstance(error, OperationalError)
            and getattr(error.__cause__, "sqlstate", None) == "55P03"
        ):
            raise SessionBusyError from None
        raise

    def require_locked_connection(
        execute: Callable[..., Any], sql: str, params: Any, many: bool, context: Any
    ) -> Any:
        if connection.connection is not locked_connection:
            raise SessionInterrupted("The session serialization connection changed.")
        return execute(sql, params, many, context)

    try:
        with connection.execute_wrapper(require_locked_connection):
            yield
    finally:
        if connection.connection is not locked_connection or not connection.is_usable():
            connection.close()
            raise SessionInterrupted("The session serialization connection was lost.")
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
                released = cursor.fetchone()[0]
            if not released:
                connection.close()
                raise SessionInterrupted("The session serialization lock was lost.")
        except BaseException:
            connection.close()
            raise


class SessionRequestSerialization:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        session_key = request.COOKIES.get(settings.SESSION_COOKIE_NAME, "")
        if re.fullmatch(r"[a-z0-9]{32}", session_key) is None:
            return self.get_response(request)
        try:
            with serialize_session(session_key):
                return self.get_response(request)
        except SessionBusyError:
            return JsonResponse(
                {"code": "session_busy", "detail": "Another session request is still running."},
                status=429,
                headers={"Retry-After": "1"},
            )
