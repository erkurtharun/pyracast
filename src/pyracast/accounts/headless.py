import json
from typing import Any

from allauth.account.internal.flows.code_verification import AbstractCodeVerificationProcess
from allauth.account.internal.flows.email_verification_by_code import EmailVerificationProcess
from allauth.account.internal.flows.password_reset_by_code import (
    PasswordResetVerificationProcess,
)
from allauth.account.internal.flows.reauthentication import raise_if_reauthentication_required
from allauth.headless.account import views
from allauth.headless.base.response import ConflictResponse
from django.http import HttpRequest, HttpResponse, HttpResponseBase

from pyracast.accounts import inputs


def orphaned(process: AbstractCodeVerificationProcess) -> bool:
    return bool(process.state.get("user_id")) and process.user is None


def discard_settled_verification(request: HttpRequest) -> None:
    # allauth finishes such a process against an account or address that changed under it
    # and answers 500, or resends a code for an address that is already verified.
    process = EmailVerificationProcess.resume(request)
    if process is None:
        return
    settled = (
        process.user is not None
        and not process.state.get("account_already_exists")
        and process.email_address.verified
    )
    if settled or orphaned(process):
        process.abort()


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

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        process = PasswordResetVerificationProcess.resume(request)
        if process is not None and orphaned(process):
            process.abort()
        return super().handle(request, *args, **kwargs)


class VerifyEmailView(views.VerifyEmailView):
    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        discard_settled_verification(request)
        return super().handle(request, *args, **kwargs)


class ResendEmailVerificationCodeView(views.ResendEmailVerificationCodeView):
    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        discard_settled_verification(request)
        return super().handle(request, *args, **kwargs)


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

    def patch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if self.input.cleaned_data["email"].primary:
            raise_if_reauthentication_required(request, app="allauth.account")
            return self._respond_email_list()
        return super().patch(request, *args, **kwargs)
