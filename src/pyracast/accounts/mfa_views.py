from typing import Any, cast

from allauth.account.internal.stagekit import LOGIN_SESSION_KEY, clear_login
from allauth.headless.base.response import UnauthorizedResponse
from allauth.headless.internal.restkit.inputs import Input
from allauth.headless.mfa import views
from allauth.headless.mfa.views import ManageRecoveryCodesView as AllauthRecoveryCodesView
from allauth.headless.mfa.views import ManageTOTPView as AllauthTOTPView
from django.contrib.auth.models import User
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
    account_state,
    confirmation,
    credential_write,
    require_authority,
)
from pyracast.accounts.security import Factor
from pyracast.accounts.sessions import revoke_other_sessions
from pyracast.accounts.step_up import StepUpAnswers, StrongestFactor, require_recent_mfa


def end_other_sessions_on_change(request: HttpRequest, authorized: str, trigger: str) -> None:
    # allauth signals an activation and a regeneration only after the commit; the other sessions
    # end in the write's transaction, so the change never commits while they stay signed in.
    user = cast(User, request.user)
    if account_state(user.pk) != authorized:
        revoke_other_sessions(request, user, trigger)


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

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method != "POST" or not request.user.is_authenticated:
            return super().handle(request, *args, **kwargs)
        with confirmation(request):
            return super().handle(request, *args, **kwargs)


class ManageRecoveryCodesView(StepUpAnswers, AllauthRecoveryCodesView):
    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method not in {"GET", "HEAD", "POST"} or not request.user.is_authenticated:
            return super().handle(request, *args, **kwargs)
        with credential_write(request) as authorized:
            refused = require_recent_mfa(request)
            if refused is not None:
                return refused
            response = super().handle(request, *args, **kwargs)
            if request.method == "POST":
                end_other_sessions_on_change(request, authorized, "mfa_reset")
            return response


class ManageTOTPView(StepUpAnswers, AllauthTOTPView):
    def get_input_class(self) -> type[Input] | None:
        return TOTPActivationInput if self.request.method == "POST" else None

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        user = request.user
        if user.is_authenticated and requires_admin_mfa(user):
            raise PermissionDenied("Administrator authenticators are provisioned by the operator.")
        if request.method not in {"POST", "DELETE"} or not user.is_authenticated:
            return super().handle(request, *args, **kwargs)
        with credential_write(request) as authorized:
            refused = require_recent_mfa(request) if request.method == "DELETE" else None
            if refused is not None:
                return refused
            response = super().handle(request, *args, **kwargs)
            if request.method == "POST":
                end_other_sessions_on_change(request, authorized, "mfa_added")
            return response
