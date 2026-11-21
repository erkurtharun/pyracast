from typing import Any, cast

from allauth.account import app_settings as account_settings
from allauth.account.internal.flows.login import (
    AUTHENTICATION_METHODS_SESSION_KEY,
    get_authentication_records,
)
from allauth.account.internal.flows.reauthentication import raise_if_reauthentication_required
from allauth.account.internal.stagekit import LOGIN_SESSION_KEY, clear_login
from allauth.core.exceptions import ReauthenticationRequired
from allauth.headless.base.response import UnauthorizedResponse
from allauth.headless.internal.restkit.inputs import Input
from allauth.headless.mfa import views
from allauth.headless.mfa.response import AuthenticatorResponse
from allauth.headless.mfa.views import ManageRecoveryCodesView as AllauthRecoveryCodesView
from allauth.headless.mfa.views import ManageTOTPView as AllauthTOTPView
from allauth.mfa.internal.flows.add import validate_can_add_authenticator
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal import flows as totp_flows
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase

from pyracast.accounts.adapters import RECOVERY_CODES_GENERATED, TOTP_ACTIVATED, AccountAdapter
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


def finish_factor_change(request: HttpRequest, authorized: str, trigger: str, notice: str) -> None:
    # allauth signals and mails an activation and a regeneration only after the commit; the other
    # sessions end and the notice is stored in the write's transaction, so the change never
    # commits while they stay signed in or without its notice, and a session signed in after the
    # commit stays.
    user = cast(User, request.user)
    if account_state(user.pk) != authorized:
        revoke_other_sessions(request, user, trigger)
        AccountAdapter().notify(notice, user)


def may_add_authenticator(user: User) -> bool:
    try:
        validate_can_add_authenticator(user)
    except ValidationError:
        return False
    return True


def shown_once(generated: Authenticator) -> list[str]:
    # Recovery codes are shown only by the answer that created them.
    codes = RecoveryCodes(generated)
    codes.mark_as_viewed()
    return list(codes.get_unused_codes())


def judged_confirmation(request: HttpRequest) -> float:
    # allauth reads the clock once to find the session's last confirmation live; the request keeps
    # that confirmation's time instead of judging it again.
    raise_if_reauthentication_required(request, app="allauth.mfa")
    flagged = [
        record
        for record in get_authentication_records(request, cast(User, request.user))
        if record.get("reauthenticated")
        and record.get("method") in account_settings.REAUTHENTICATION_METHODS
    ]
    return float(flagged[-1]["at"])


def record_activation_proof(
    request: HttpRequest, authenticator: Authenticator, confirmed_at: float
) -> None:
    # The activation code proves the new authenticator, so it counts as a confirmation by it,
    # within the window of the confirmation that allowed the activation and never longer.
    records = get_authentication_records(request, cast(User, request.user))
    records.append(
        {
            "method": Factor.MFA.value,
            "type": authenticator.type,
            "id": authenticator.pk,
            "at": confirmed_at,
            "reauthenticated": True,
        }
    )
    request.session[AUTHENTICATION_METHODS_SESSION_KEY] = records


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
                finish_factor_change(request, authorized, "mfa_reset", RECOVERY_CODES_GENERATED)
            return response


class ManageTOTPView(StepUpAnswers, AllauthTOTPView):
    confirmed_at: float | None = None

    def get_input_class(self) -> type[Input] | None:
        return TOTPActivationInput if self.request.method == "POST" else None

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        user = request.user
        if user.is_authenticated and requires_admin_mfa(user):
            raise PermissionDenied("Administrator authenticators are provisioned by the operator.")
        # The secret is issued, and a code read, only for a session that confirmed itself, so the
        # first code activates; an account that may not add one is told so first.
        enrolling = (
            request.method in {"GET", "POST"}
            and self._get_authenticator() is None
            and may_add_authenticator(cast(User, user))
        )
        if request.method == "GET" and enrolling:
            raise_if_reauthentication_required(request, app="allauth.mfa")
        if request.method not in {"POST", "DELETE"} or not user.is_authenticated:
            return super().handle(request, *args, **kwargs)
        with credential_write(request) as authorized:
            if enrolling:
                self.confirmed_at = judged_confirmation(request)
            refused = require_recent_mfa(request) if request.method == "DELETE" else None
            if refused is not None:
                return refused
            response = super().handle(request, *args, **kwargs)
            if request.method == "POST":
                finish_factor_change(request, authorized, "mfa_added", TOTP_ACTIVATED)
            return response

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if self.confirmed_at is None:
            raise ReauthenticationRequired
        authenticator, generated = totp_flows.activate_totp(request, self.input)
        record_activation_proof(request, authenticator, self.confirmed_at)
        meta = None if generated is None else {"recovery_codes": shown_once(generated)}
        return AuthenticatorResponse(request, authenticator, meta=meta)
