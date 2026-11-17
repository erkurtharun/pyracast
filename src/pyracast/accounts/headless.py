import json
from typing import Any

from allauth.headless.account import views
from allauth.headless.base.response import ConflictResponse
from django.http import HttpRequest, HttpResponse, HttpResponseBase

from pyracast.accounts import inputs


class LoginView(views.LoginView):
    input_class = inputs.LoginInput

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method == "POST" and request.user.is_authenticated:
            return ConflictResponse(request)
        return super().handle(request, *args, **kwargs)


class RequestPasswordResetView(views.RequestPasswordResetView):
    input_class = inputs.RequestPasswordResetInput


class ResetPasswordView(views.ResetPasswordView):
    input_class = inputs.ResetPasswordInput


class ChangePasswordView(views.ChangePasswordView):
    input_class = inputs.ChangePasswordInput

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        response: HttpResponse = super().post(request, *args, **kwargs)
        payload = json.loads(response.content)
        payload.setdefault("meta", {})["password_changed"] = True
        response.content = json.dumps(payload)
        return response


class ReauthenticateView(views.ReauthenticateView):
    input_class = inputs.ReauthenticateInput
