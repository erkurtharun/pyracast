import time
from typing import Any, cast

from allauth.account.internal.flows.password_reset_by_code import PasswordResetVerificationProcess
from allauth.account.internal.flows.reauthentication import raise_if_reauthentication_required
from allauth.account.internal.stagekit import LOGIN_SESSION_KEY
from allauth.account.models import EmailAddress
from allauth.headless.base.response import ConflictResponse, ForbiddenResponse
from allauth.headless.base.views import APIView
from allauth.headless.internal.restkit.inputs import Input
from allauth.headless.mfa import inputs, views
from allauth.headless.mfa.response import (
    AddWebAuthnResponse,
    AuthenticatorResponse,
    WebAuthnRequestOptionsResponse,
)
from allauth.mfa.adapter import get_adapter
from allauth.mfa.models import Authenticator
from allauth.mfa.webauthn.internal import auth, flows
from django import forms
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from fido2.utils import websafe_decode
from fido2.webauthn import CollectedClientData

from pyracast.accounts.adapters import acting_on_judged_confirmation
from pyracast.accounts.authorization import requires_admin_mfa
from pyracast.accounts.mfa_views import PendingFactorView, shown_once
from pyracast.accounts.processes import (
    PROVIDER_SIGNUP_SESSION_KEY,
    StartingView,
    confirmation,
    credential_write,
    end_confirmations,
    lock_account,
)
from pyracast.accounts.security import Factor
from pyracast.accounts.sessions import revoke_other_sessions
from pyracast.accounts.step_up import StepUpAnswers, StrongestFactor, require_recent_mfa


def invalid_passkey() -> ValidationError:
    return get_adapter().validation_error("incorrect_code")


def active_customer(user: User) -> bool:
    return User.objects.filter(
        pk=user.pk, is_active=True, is_staff=False, is_superuser=False
    ).exists()


def validate_passkey_origin(credential: Any) -> None:
    # fido2 verifies the RP ID, signature, challenge and UV. Pyracast accepts credentials only
    # from its configured app origin, including its port, and only in a first-party context.
    try:
        data = CollectedClientData(websafe_decode(credential["response"]["clientDataJSON"]))
        if data.origin != settings.PYRACAST_PUBLIC_ORIGIN or data.cross_origin:
            raise invalid_passkey()
        if data.type == CollectedClientData.TYPE.CREATE:
            auth.parse_registration_response(credential)
        else:
            auth.parse_authentication_response(credential)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise invalid_passkey() from error


class PasskeyInput(forms.Form):
    credential = forms.JSONField(validators=[validate_passkey_origin])


class EnrollPasskeyInput(PasskeyInput, inputs.AddWebAuthnInput):
    name = forms.CharField(required=False, max_length=100)


class LoginPasskeyInput(PasskeyInput, inputs.LoginWebAuthnInput):
    def clean_credential(self) -> Authenticator:
        verified = super().clean_credential()
        # Only a valid assertion identifies an account to lock. Re-read the credential and its
        # owner under those locks before issuing a session, including after an operator promotion.
        lock_account(verified.user_id)
        current: Authenticator | None = (
            Authenticator.objects.select_related("user")
            .filter(
                pk=verified.pk,
                user_id=verified.user_id,
                type=Authenticator.Type.WEBAUTHN,
                data=verified.data,
                user__is_active=True,
                user__is_staff=False,
                user__is_superuser=False,
            )
            .first()
        )
        if current is None:
            raise invalid_passkey()
        return current


class AuthenticatePasskeyInput(PasskeyInput, inputs.AuthenticateWebAuthnInput):
    pass


class ReauthenticatePasskeyInput(PasskeyInput, inputs.ReauthenticateWebAuthnInput):
    pass


class PasskeyCeremony(APIView):
    answer_input: type[Input]

    def get_input_class(self) -> type[Input] | None:
        return self.answer_input if self.request.method == "POST" else super().get_input_class()

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        # allauth keeps one challenge per session. Bind it to this operation and a short
        # lifetime. A submitted answer consumes it, including when validation fails.
        if request.method == "POST":
            state = auth.get_state() or {}
            try:
                if state.get("path") != request.path or time.time() >= state.get("expires", 0):
                    return ConflictResponse(request)
                return super().handle(request, *args, **kwargs)
            finally:
                auth.clear_state()
        response = super().handle(request, *args, **kwargs)
        if request.method == "GET" and response.status_code == 200:
            state = request.session[auth.STATE_SESSION_KEY]
            auth.set_state({**state, "path": request.path, "expires": time.time() + 300})
        return response


AUTOFILL_STATE = "pyracast.passkey_autofill"


class LoginCeremony(PasskeyCeremony):
    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.user.is_authenticated or LOGIN_SESSION_KEY in request.session:
            return ConflictResponse(request)
        if "autofill" not in request.GET:
            return super().handle(request, *args, **kwargs)
        if (
            PROVIDER_SIGNUP_SESSION_KEY in request.session
            or PasswordResetVerificationProcess.resume(request)
        ):
            return ConflictResponse(request)

        # A passive tab can replace another passive request, but cannot disturb a modal
        # ceremony. Only the newest passive answer may consume the passive slot.
        saved = auth.get_state()
        if request.method == "POST":
            state = request.session.get(AUTOFILL_STATE)
            if state is None or state["challenge"] != request.GET["autofill"]:
                return ConflictResponse(request)
            auth.set_state(request.session.pop(AUTOFILL_STATE))
        try:
            response = super().handle(request, *args, **kwargs)
            if request.method == "GET" and response.status_code == 200:
                request.session[AUTOFILL_STATE] = request.session[auth.STATE_SESSION_KEY]
            return response
        finally:
            if (
                saved
                and not request.user.is_authenticated
                and LOGIN_SESSION_KEY not in request.session
            ):
                auth.set_state(saved)
            else:
                auth.clear_state()


class LoginView(StartingView, LoginCeremony, views.LoginWebAuthnView):
    answer_input = LoginPasskeyInput


class AuthenticateView(PendingFactorView, views.AuthenticateWebAuthnView, PasskeyCeremony):
    answer_input = AuthenticatePasskeyInput

    def get_input_kwargs(self) -> dict[str, Any]:
        user = cast(User, self.stage.login.user)
        if requires_admin_mfa(user):
            raise PermissionDenied
        return {"user": user}

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return WebAuthnRequestOptionsResponse(
            request, auth.begin_authentication(self.get_input_kwargs()["user"])
        )


class ReauthenticateView(StrongestFactor, PasskeyCeremony, views.ReauthenticateWebAuthnView):
    factor = Factor.MFA
    answer_input = ReauthenticatePasskeyInput

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        with confirmation(request):
            if not active_customer(cast(User, request.user)):
                return ForbiddenResponse(request)
            return super().handle(request, *args, **kwargs)


class ManageView(StepUpAnswers, PasskeyCeremony, views.ManageWebAuthnView):
    answer_input = EnrollPasskeyInput

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        with credential_write(request):
            user = cast(User, request.user)
            if not active_customer(user):
                return ForbiddenResponse(request)
            if request.method == "DELETE" and (refused := require_recent_mfa(request)) is not None:
                return refused
            if (
                request.method in {"GET", "POST"}
                and not EmailAddress.objects.filter(user=user, verified=True, primary=True).exists()
            ):
                return ForbiddenResponse(request)
            raise_if_reauthentication_required(request, app="allauth.mfa")
            with acting_on_judged_confirmation():
                return super().handle(request, *args, **kwargs)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        options = flows.begin_registration(request, request.user, passwordless=True)
        return AddWebAuthnResponse(request, options)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        authenticator, generated = flows.add_authenticator(
            request,
            name=self.input.cleaned_data["name"],
            credential=self.input.cleaned_data["credential"],
        )
        user = cast(User, request.user)
        revoke_other_sessions(request, user, "mfa_added")
        end_confirmations(request, user)
        meta = None if generated is None else {"recovery_codes": shown_once(generated)}
        return AuthenticatorResponse(request, authenticator, meta=meta)

    def delete(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        response = super().delete(request, *args, **kwargs)
        end_confirmations(request, cast(User, request.user))
        return response
