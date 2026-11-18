import time
from typing import Any

from allauth.account import app_settings
from allauth.account.internal.flows.login import get_authentication_records
from allauth.account.internal.stagekit import LOGIN_SESSION_KEY, clear_login
from allauth.headless.base.response import UnauthorizedResponse
from allauth.headless.internal.restkit.inputs import Input
from allauth.headless.internal.restkit.response import APIResponse
from allauth.headless.mfa import views
from allauth.headless.mfa.views import ManageRecoveryCodesView as AllauthRecoveryCodesView
from allauth.headless.mfa.views import ManageTOTPView as AllauthTOTPView
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpRequest, HttpResponseBase
from django.utils.translation import gettext

from pyracast.accounts.authorization import requires_admin_mfa
from pyracast.accounts.inputs import (
    SecondFactorInput,
    SecondFactorReauthenticateInput,
    TOTPActivationInput,
)
from pyracast.accounts.processes import AuthorityChangedError, require_authority


def recently_reauthenticated_with_mfa(request: HttpRequest) -> bool:
    user = request.user
    if not user.is_authenticated:
        return False
    now = time.time()
    authenticator_ids = []
    for record in get_authentication_records(request, user):
        at = record.get("at")
        if (
            record.get("method") == "mfa"
            and record.get("type") in {"totp", "recovery_codes"}
            and record.get("reauthenticated")
            and isinstance(at, (int, float))
            and 0 <= now - at < app_settings.REAUTHENTICATION_TIMEOUT
        ):
            authenticator_ids.append(record.get("id"))
    return Authenticator.objects.filter(user=user, pk__in=authenticator_ids).exists()


def require_mfa_reauthentication(request: HttpRequest) -> APIResponse | None:
    user = request.user
    if user.is_authenticated and not recently_reauthenticated_with_mfa(request):
        return APIResponse(
            request,
            status=401,
            meta={"is_authenticated": True},
            errors=[
                {
                    "code": "mfa_reauthentication_required",
                    "message": gettext(
                        "Verify an authenticator before managing two-factor authentication."
                    ),
                }
            ],
        )
    return None


class AuthenticateView(views.AuthenticateView):
    input_class = SecondFactorInput

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method != "POST":
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


class ReauthenticateView(views.ReauthenticateView):
    input_class = SecondFactorReauthenticateInput


class ManageRecoveryCodesView(AllauthRecoveryCodesView):
    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method not in {"GET", "HEAD", "POST"} or not request.user.is_authenticated:
            return super().handle(request, *args, **kwargs)
        with transaction.atomic():
            request.user = User.objects.select_for_update().get(pk=request.user.pk)
            response = require_mfa_reauthentication(request)
            if response is not None:
                return response
            return super().handle(request, *args, **kwargs)


class ManageTOTPView(AllauthTOTPView):
    def get_input_class(self) -> type[Input] | None:
        return TOTPActivationInput if self.request.method == "POST" else None

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method not in {"POST", "DELETE"} or not request.user.is_authenticated:
            return super().handle(request, *args, **kwargs)
        with transaction.atomic():
            request.user = User.objects.select_for_update().get(pk=request.user.pk)
            if request.method == "DELETE":
                if requires_admin_mfa(request.user):
                    raise PermissionDenied("Administrator authenticators cannot be removed.")
                response = require_mfa_reauthentication(request)
                if response is not None:
                    return response
            return super().handle(request, *args, **kwargs)
