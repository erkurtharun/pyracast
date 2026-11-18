import hashlib
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from django.conf import settings
from django.contrib.sessions.exceptions import SessionInterrupted
from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError, connections, transaction
from django.http import HttpRequest, HttpResponse
from django.utils import translation
from django.utils.cache import patch_cache_control
from django.utils.html import format_html

from pyracast.accounts.language import request_language
from pyracast.api.errors import coded, error_json
from pyracast.errors import MESSAGES, ErrorCode

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
        # The wait applies to this transaction only; the advisory lock outlives it.
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT set_config('lock_timeout', %s, true)", [_LOCK_TIMEOUT])
            cursor.execute("SELECT pg_advisory_lock(%s)", [lock_id])
            locked_connection = connection.connection
    except OperationalError as error:
        if getattr(error.__cause__, "sqlstate", None) != "55P03":
            connection.close()
            raise
        raise SessionBusyError from None
    except BaseException:
        connection.close()
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
            if (
                request.method == "GET"
                and request.headers.get("Sec-Fetch-Mode") == "navigate"
                and not request.path.startswith("/api/")
            ):
                return _busy_page(request)
            return error_json(request, lambda: coded(ErrorCode.SESSION_BUSY, 429, retry_after=1))


def _busy_page(request: HttpRequest) -> HttpResponse:
    with translation.override(request_language(request, None)):
        page = format_html(
            '<!doctype html><html lang="{}"><title>Pyracast</title><p>{}</p></html>',
            translation.get_language(),
            MESSAGES[ErrorCode.SESSION_BUSY],
        )
    response = HttpResponse(page, status=503)
    if "session_retry" not in request.GET:
        path = "/" + request.get_full_path().lstrip("/")
        # One automatic reload: the marker stops a refresh loop while the session stays busy.
        response["Refresh"] = f"1; url={path}{'&' if '?' in path else '?'}session_retry=1"
    patch_cache_control(response, private=True, no_store=True)
    return response
