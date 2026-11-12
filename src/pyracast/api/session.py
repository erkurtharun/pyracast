from typing import Any

from django.contrib.auth.models import User
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from pyracast.accounts.security import has_verified_mfa


class SessionUserOutput(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField(allow_blank=True)
    can_admin = serializers.SerializerMethodField()

    def get_can_admin(self, user: User) -> bool:
        return bool(
            user.is_staff and user.is_superuser and has_verified_mfa(self.context["request"])
        )


class SessionOutput(serializers.Serializer[Any]):
    user = SessionUserOutput(allow_null=True)
    csrf_token = serializers.CharField()


@extend_schema(auth=[])
@method_decorator(never_cache, name="dispatch")
class SessionView(APIView):
    permission_classes = (AllowAny,)

    @extend_schema(responses=SessionOutput)
    def get(self, request: Request) -> Response:
        user = request.user
        return Response(
            {
                "user": (
                    SessionUserOutput(user, context={"request": request._request}).data
                    if user.is_authenticated
                    else None
                ),
                "csrf_token": get_token(request._request),
            }
        )
