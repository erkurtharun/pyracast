from typing import Any

from allauth.account.internal.stagekit import LOGIN_SESSION_KEY, clear_login
from allauth.headless.base.response import UnauthorizedResponse
from allauth.headless.internal.restkit.inputs import Input
from allauth.headless.mfa import views
from allauth.headless.mfa.views import ManageRecoveryCodesView as AllauthRecoveryCodesView
from allauth.headless.mfa.views import ManageTOTPView as AllauthTOTPView
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpRequest, HttpResponseBase

from pyracast.accounts.authorization import requires_admin_mfa
from pyracast.accounts.inputs import (
    SecondFactorInput,
    SecondFactorReauthenticateInput,
    TOTPActivationInput,
)
from pyracast.accounts.processes import (
    AuthorityChangedError,
    require_authority,
    require_current_proof,
)
from pyracast.accounts.security import Factor
from pyracast.accounts.step_up import StepUpAnswers, StrongestFactor, require_recent_mfa


class AuthenticateView(views.AuthenticateView):
    input_class = SecondFactorInput

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        # Without a pending sign-in allauth answers and the session keeps its authentication.
        if request.method != "POST" or LOGIN_SESSION_KEY not in request.session:
            return super().handle(request, *args, **kwargs)
        # The step is checked under the account's locks before its input uses up a code, so the
        # code, the authenticator write and the sign-in commit together.
        try:
            with transaction.atomic():
                require_authority(request, LOGIN_SESSION_KEY)
                return super().handle(request, *args, **kwargs)
        except AuthorityChangedError:
            clear_login(request)
            return UnauthorizedResponse(request)


class ReauthenticateView(StrongestFactor, views.ReauthenticateView):
    factor = Factor.MFA
    input_class = SecondFactorReauthenticateInput


class ManageRecoveryCodesView(StepUpAnswers, AllauthRecoveryCodesView):
    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method not in {"GET", "HEAD", "POST"} or not request.user.is_authenticated:
            return super().handle(request, *args, **kwargs)
        with transaction.atomic():
            require_current_proof(request)
            response = require_recent_mfa(request)
            if response is not None:
                return response
            return super().handle(request, *args, **kwargs)


class ManageTOTPView(StepUpAnswers, AllauthTOTPView):
    def get_input_class(self) -> type[Input] | None:
        return TOTPActivationInput if self.request.method == "POST" else None

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        user = request.user
        if user.is_authenticated and requires_admin_mfa(user):
            raise PermissionDenied("Administrator authenticators are provisioned by the operator.")
        if request.method not in {"POST", "DELETE"} or not user.is_authenticated:
            return super().handle(request, *args, **kwargs)
        with transaction.atomic():
            require_current_proof(request)
            response = require_recent_mfa(request) if request.method == "DELETE" else None
            if response is not None:
                return response
            return super().handle(request, *args, **kwargs)
