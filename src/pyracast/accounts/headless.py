import json
from typing import Any

from allauth.headless.account import views
from django.http import HttpRequest, HttpResponse

from pyracast.accounts import inputs


class LoginView(views.LoginView):
    input_class = inputs.LoginInput


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


class ManageEmailView(views.ManageEmailView):
    input_class = views.ManageEmailView.input_class | {"PUT": inputs.ResendEmailVerificationInput}
