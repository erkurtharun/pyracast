from typing import Any

from django.contrib.auth.models import User
from django.middleware.csrf import get_token
from django.utils import translation
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from pyracast.accounts.language import (
    LANGUAGES,
    save_user_language,
    set_language_cookie,
    user_language,
)
from pyracast.accounts.security import has_verified_mfa


class SessionUserOutput(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField(allow_blank=True)
    can_admin = serializers.SerializerMethodField()
    language_preference = serializers.SerializerMethodField()

    def get_language_preference(self, user: User) -> str | None:
        return user_language(user)

    def get_can_admin(self, user: User) -> bool:
        return bool(
            user.is_staff and user.is_superuser and has_verified_mfa(self.context["request"])
        )


class SessionOutput(serializers.Serializer[Any]):
    user = SessionUserOutput(allow_null=True)
    csrf_token = serializers.CharField()
    language = serializers.ChoiceField(choices=LANGUAGES)


def session_output(request: Request) -> dict[str, Any]:
    user = request.user
    return {
        "user": (
            SessionUserOutput(user, context={"request": request._request}).data
            if user.is_authenticated
            else None
        ),
        "csrf_token": get_token(request._request),
        "language": request._request.LANGUAGE_CODE,
    }


@extend_schema(auth=[])
@method_decorator(never_cache, name="dispatch")
class SessionView(APIView):
    permission_classes = (AllowAny,)

    @extend_schema(responses=SessionOutput)
    def get(self, request: Request) -> Response:
        return Response(session_output(request))


class LanguageInput(serializers.Serializer[Any]):
    language = serializers.ChoiceField(choices=LANGUAGES)


@extend_schema(auth=[])
@method_decorator(never_cache, name="dispatch")
@method_decorator(csrf_protect, name="dispatch")
class SessionLanguageView(APIView):
    permission_classes = (AllowAny,)

    @extend_schema(request=LanguageInput, responses=SessionOutput)
    def post(self, request: Request) -> Response:
        payload = LanguageInput(data=request.data)
        payload.is_valid(raise_exception=True)
        language = payload.validated_data["language"]
        if request.user.is_authenticated:
            save_user_language(request.user, language)
        request._request.LANGUAGE_CODE = language
        translation.activate(language)
        response = Response(session_output(request))
        set_language_cookie(response, language)
        return response
