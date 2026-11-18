from drf_spectacular.authentication import SessionScheme
from rest_framework import authentication, exceptions
from rest_framework.request import Request

from pyracast.errors import ErrorCode, ForbiddenError


class SessionAuthentication(authentication.SessionAuthentication):
    def authenticate_header(self, request: Request) -> str:
        return "Session"

    def enforce_csrf(self, request: Request) -> None:
        try:
            super().enforce_csrf(request)
        except exceptions.PermissionDenied as error:
            raise ForbiddenError(ErrorCode.CSRF_FAILED) from error


# Creating the subclass registers the schema extension; `type()` keeps the untyped hook out of
# strict typing.
SessionAuthenticationScheme = type(
    "SessionAuthenticationScheme",
    (SessionScheme,),
    {"target_class": "pyracast.api.authentication.SessionAuthentication"},
)
