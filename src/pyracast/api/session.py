from typing import Any

from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView


class SessionUserOutput(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField(allow_blank=True)


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
                "user": SessionUserOutput(user).data if user.is_authenticated else None,
                "csrf_token": get_token(request._request),
            }
        )
