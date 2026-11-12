from typing import Any, cast

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.middleware.csrf import get_token, rotate_token
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated, PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from pyracast.accounts.registration import register_company
from pyracast.api.serializers import InputSerializer
from pyracast.notifications.operations import recipient_limit


class BrowserSessionAuthentication(SessionAuthentication):
    def authenticate(self, request: Request) -> tuple[Any, Any] | None:
        self.enforce_csrf(request)
        return super().authenticate(request)


class SessionUserOutput(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField(allow_blank=True)


class SessionOutput(serializers.Serializer[Any]):
    user = SessionUserOutput(allow_null=True)
    csrf_token = serializers.CharField()


class LoginInput(InputSerializer):
    username = serializers.CharField(max_length=150, trim_whitespace=False)
    password = serializers.CharField(max_length=1024, write_only=True, trim_whitespace=False)


class EmailInput(InputSerializer):
    email = serializers.EmailField(max_length=254)


class RegistrationInput(LoginInput):
    company_name = serializers.CharField(max_length=200)
    email = serializers.EmailField(max_length=254)


def session_response(request: Request) -> Response:
    user = request._request.user
    return Response(
        {
            "user": SessionUserOutput(user).data if user.is_authenticated else None,
            "csrf_token": get_token(request._request),
        }
    )


@method_decorator(never_cache, name="dispatch")
@method_decorator(sensitive_post_parameters("password"), name="dispatch")
class BrowserSessionView(APIView):
    authentication_classes = (BrowserSessionAuthentication,)
    permission_classes = (AllowAny,)


@extend_schema(auth=[])
class SessionView(BrowserSessionView):
    @extend_schema(responses=SessionOutput)
    def get(self, request: Request) -> Response:
        return session_response(request)

    @extend_schema(
        request=EmailInput, responses=SessionOutput, auth=cast(Any, [{"cookieAuth": []}])
    )
    def patch(self, request: Request) -> Response:
        if not request.user.is_authenticated:
            raise NotAuthenticated()
        data = EmailInput(data=request.data)
        data.is_valid(raise_exception=True)
        user = request.user
        with transaction.atomic():
            recipient_limit(lock=True)
            user.email = data.validated_data["email"]
            user.save(update_fields=["email"])
        return session_response(request)


@extend_schema(auth=[])
class SessionLoginView(BrowserSessionView):
    @extend_schema(request=LoginInput, responses=SessionOutput)
    def post(self, request: Request) -> Response:
        data = LoginInput(data=request.data)
        data.is_valid(raise_exception=True)
        user = authenticate(request._request, **data.validated_data)
        if user is None:
            raise AuthenticationFailed("Invalid credentials.")
        login(request._request, user)
        return session_response(request)


@extend_schema(auth=[])
class SessionRegisterView(BrowserSessionView):
    @extend_schema(request=RegistrationInput, responses={201: SessionOutput})
    def post(self, request: Request) -> Response:
        if request.user.is_authenticated:
            raise PermissionDenied("Sign out before creating a new company account.")
        data = RegistrationInput(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            user = register_company(**data.validated_data)
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message_dict) from None
        login(request._request, user, backend="django.contrib.auth.backends.ModelBackend")
        response = session_response(request)
        response.status_code = 201
        return response


@extend_schema(auth=[])
class SessionLogoutView(BrowserSessionView):
    @extend_schema(request=None, responses=SessionOutput)
    def post(self, request: Request) -> Response:
        logout(request._request)
        rotate_token(request._request)
        return session_response(request)
