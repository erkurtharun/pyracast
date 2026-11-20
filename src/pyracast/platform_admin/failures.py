import logging
from collections.abc import Callable
from functools import update_wrapper
from typing import Any, cast

from django.contrib import messages
from django.db import DatabaseError
from django.http import HttpRequest, JsonResponse
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy

from pyracast.errors import ErrorCode, retryable_database_code
from pyracast.platform_admin.navigation import SAFE_METHODS, console_destination

logger = logging.getLogger("pyracast.platform_admin")
_CONFLICT = gettext_lazy(
    "Another change to this record is in progress, so nothing was saved. Try again in a moment."
)
RETRY_MESSAGES = {
    ErrorCode.BUSY: _CONFLICT,
    ErrorCode.DATABASE_CONFLICT: _CONFLICT,
    ErrorCode.REQUEST_TIMEOUT: gettext_lazy(
        "The change took too long, so nothing was saved. Try again in a moment."
    ),
}


def wants_json(request: HttpRequest) -> bool:
    return request.headers.get("Accept") == "application/json"


def contained[V: Callable[..., Any]](view: V) -> V:
    def answer(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        try:
            return view(request, *args, **kwargs)
        except DatabaseError as error:
            code = retryable_database_code(error)
            if code is None or request.method in SAFE_METHODS:
                raise
            logger.warning(
                "console_write_not_saved code=%s view=%s",
                code.value,
                getattr(request.resolver_match, "view_name", "-"),
            )
            if wants_json(request):
                return JsonResponse(
                    {"message": str(RETRY_MESSAGES[code])},
                    status=503,
                    headers={"Retry-After": "1"},
                )
            messages.error(request, RETRY_MESSAGES[code])
            return redirect(console_destination(request, request.headers.get("Referer")))

    return cast(V, update_wrapper(answer, view))
