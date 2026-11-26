from datetime import datetime
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.http import HttpRequest
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from pyracast.accounts.authorization import has_platform_admin_session, requires_admin_mfa
from pyracast.accounts.language import (
    save_user_language,
    set_language_cookie,
    user_language,
)
from pyracast.accounts.models import Language
from pyracast.accounts.session_tag import session_tag
from pyracast.accounts.sessions import absolute_end
from pyracast.api.serializers import InputSerializer


class SessionUserOutput(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField(allow_blank=True)
    has_usable_password = serializers.BooleanField(read_only=True)
    can_admin = serializers.SerializerMethodField()
    mfa_required = serializers.SerializerMethodField()
    language_preference = serializers.SerializerMethodField()

    @staticmethod
    def get_language_preference(user: User) -> str | None:
        return user_language(user)

    @staticmethod
    def get_mfa_required(user: User) -> bool:
        return requires_admin_mfa(user)

    def get_can_admin(self, user: User) -> bool:
        return has_platform_admin_session(self.context["request"])


class SessionOutput(serializers.Serializer[Any]):
    user = SessionUserOutput(allow_null=True)
    csrf_token = serializers.CharField()
    language = serializers.ChoiceField(choices=Language.choices)
    expires_at = serializers.DateTimeField(allow_null=True)
    idle_seconds = serializers.IntegerField(allow_null=True)
    session_tag = serializers.CharField(allow_null=True)


def _limits(request: Request) -> dict[str, datetime | int | None]:
    if not request.user.is_authenticated:
        return {"expires_at": None, "idle_seconds": None}
    # SessionLifetime has already ended a session without a valid start time.
    return {
        "expires_at": absolute_end(request.session),
        "idle_seconds": settings.PYRACAST_SESSION_IDLE_SECONDS,
    }


def http_request(request: Request) -> HttpRequest:
    """The Django request behind a DRF request: the language middleware reads its
    `LANGUAGE_CODE` after the view, and Django helpers expect it."""
    return request._request


def session_output(request: Request) -> dict[str, Any]:
    user = request.user
    django_request = http_request(request)
    return {
        "user": (
            SessionUserOutput(user, context={"request": django_request}).data
            if user.is_authenticated
            else None
        ),
        "csrf_token": get_token(django_request),
        "language": django_request.LANGUAGE_CODE,
        **_limits(request),
        "session_tag": session_tag(request._request),
    }


@extend_schema(auth=[])
@method_decorator(never_cache, name="dispatch")
class SessionView(APIView):
    @extend_schema(responses=SessionOutput)
    def get(self, request: Request) -> Response:
        return Response(session_output(request))


class LanguageInput(InputSerializer):
    language = serializers.ChoiceField(choices=Language.choices)


@extend_schema(auth=[])
@method_decorator(never_cache, name="dispatch")
@method_decorator(csrf_protect, name="dispatch")
class SessionLanguageView(APIView):
    @extend_schema(request=LanguageInput, responses=SessionOutput)
    def post(self, request: Request) -> Response:
        payload = LanguageInput(data=request.data)
        payload.is_valid(raise_exception=True)
        language = payload.validated_data["language"]
        if request.user.is_authenticated:
            save_user_language(request.user, language)
        http_request(request).LANGUAGE_CODE = language
        response = Response(session_output(request))
        set_language_cookie(response, language)
        return response
