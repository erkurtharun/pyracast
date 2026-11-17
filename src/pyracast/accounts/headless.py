import json
from typing import Any

from allauth.account.internal.flows.code_verification import AbstractCodeVerificationProcess
from allauth.account.internal.flows.email_verification import login_on_verification
from allauth.account.internal.flows.email_verification_by_code import EmailVerificationProcess
from allauth.account.internal.flows.reauthentication import raise_if_reauthentication_required
from allauth.account.internal.userkit import str_to_user_id
from allauth.account.models import EmailAddress
from allauth.core.exceptions import RateLimited
from allauth.headless.account import views
from allauth.headless.base.response import (
    AuthenticationResponse,
    ConflictResponse,
    RateLimitResponse,
)
from allauth.headless.internal.restkit.response import APIResponse
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase

from pyracast.accounts import inputs


def lock_account(process: AbstractCodeVerificationProcess) -> bool:
    user_id = process.state.get("user_id")
    if not user_id:
        return True
    try:
        User.objects.select_for_update().get(pk=str_to_user_id(user_id))
    except User.DoesNotExist:
        return False
    return True


def still_pending(process: EmailVerificationProcess) -> bool:
    # Locks the account, then every address row with the pending email: a verification or
    # deletion from another session has either committed before the decision or waits for the
    # caller's transaction, so allauth never completes against a state that changed under it.
    if not lock_account(process):
        return False
    email = process.state.get("email", "")
    addresses = list(EmailAddress.objects.select_for_update().filter(email__iexact=email))
    user_id = process.state.get("user_id")
    if not user_id or process.state.get("account_already_exists"):
        return True
    return not any(str(address.user_id) == user_id and address.verified for address in addresses)


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

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        with transaction.atomic():
            if self.process is not None and not lock_account(self.process):
                self.process.abort()
                return ConflictResponse(request)
            return super().post(request, *args, **kwargs)


class VerifyEmailView(views.VerifyEmailView):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        process = self.process
        if process is None:
            return super().post(request, *args, **kwargs)
        with transaction.atomic():
            email_address = process.finish() if still_pending(process) else None
        if email_address is None:
            process.abort()
            return ConflictResponse(request)
        response = login_on_verification(request, email_address) if self.stage else None
        return AuthenticationResponse.from_response(request, response)


class ResendEmailVerificationCodeView(views.ResendEmailVerificationCodeView):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        with transaction.atomic():
            process = EmailVerificationProcess.resume(request)
            if process is None or not process.can_resend:
                return ConflictResponse(request)
            if not still_pending(process):
                process.abort()
                return ConflictResponse(request)
            try:
                process.resend()
            except RateLimited:
                return RateLimitResponse(request)
        return APIResponse(request)


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
