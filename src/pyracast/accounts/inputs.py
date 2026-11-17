from typing import Any

from allauth.account.adapter import get_adapter
from allauth.core import context
from allauth.headless.account.inputs import (
    ChangePasswordInput as AllauthChangePasswordInput,
)
from allauth.headless.account.inputs import (
    LoginInput as AllauthLoginInput,
)
from allauth.headless.account.inputs import (
    ReauthenticateInput as AllauthReauthenticateInput,
)
from allauth.headless.account.inputs import (
    RequestPasswordResetInput as AllauthRequestPasswordResetInput,
)
from allauth.headless.account.inputs import (
    ResetPasswordInput as AllauthResetPasswordInput,
)
from allauth.headless.mfa import inputs as mfa_inputs
from allauth.mfa.adapter import get_adapter as get_mfa_adapter
from allauth.mfa.base.forms import BaseAuthenticateForm
from allauth.mfa.internal.flows.add import validate_can_add_authenticator
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal import auth as totp_auth
from django import forms
from django.contrib.auth.models import User
from django.core.validators import MaxLengthValidator

from pyracast.accounts.audit import record_security_event
from pyracast.accounts.authentication import email_identity
from pyracast.accounts.login_limits import check_second_factor


class PasswordInput(forms.Form):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for name in ("password", "current_password", "new_password"):
            field = self.fields.get(name)
            if isinstance(field, forms.CharField):
                field.strip = False
                field.max_length = 1024
                field.validators.append(MaxLengthValidator(1024))


class LoginInput(PasswordInput, AllauthLoginInput):
    pass


class RequestPasswordResetInput(AllauthRequestPasswordResetInput):
    users: list[User]

    def clean_email(self) -> str:
        email: str = super().clean_email()
        user = email_identity(email, for_recovery=True)
        self.users = [user] if user is not None and user.is_active else []
        return email


class ResetPasswordInput(PasswordInput, AllauthResetPasswordInput):
    pass


class ChangePasswordInput(PasswordInput, AllauthChangePasswordInput):
    user: User

    def clean_current_password(self) -> str:
        current_password: str = self.cleaned_data["current_password"]
        if current_password and not self.user.check_password(current_password):
            record_security_event("password_change_failed", request=context.request, user=self.user)
            raise get_adapter().validation_error("enter_current_password")
        return current_password


class ReauthenticateInput(PasswordInput, AllauthReauthenticateInput):
    pass


class SecondFactorCode(BaseAuthenticateForm):
    # allauth's `clean_code` without its per-user limiter, which `check_second_factor` replaces.
    def clean_code(self) -> str:
        return check_second_factor(self.user, self.matching_code)

    def matching_code(self) -> str:
        code: str = self.cleaned_data["code"]
        main: Authenticator | None = None
        for authenticator in Authenticator.objects.filter(user=self.user).exclude(
            type=Authenticator.Type.WEBAUTHN
        ):
            if not main or authenticator.type == Authenticator.Type.TOTP:
                main = authenticator
            if authenticator.wrap().validate_code(code):
                authenticator.user = self.user
                self.authenticator = authenticator
                return code
        if main:
            self._emit_authentication_failed(main)
        raise get_mfa_adapter().validation_error("incorrect_code")


class SecondFactorInput(SecondFactorCode, mfa_inputs.AuthenticateInput):
    pass


class SecondFactorReauthenticateInput(SecondFactorCode, mfa_inputs.ReauthenticateInput):
    pass


class TOTPActivationInput(mfa_inputs.ActivateTOTPInput):
    # allauth's `clean_code` with `check_second_factor` in place of its per-user limiter; enrolment
    # codes do not count towards the client allowance.
    def clean_code(self) -> str:
        validate_can_add_authenticator(self.user)
        code: str = self.cleaned_data["code"]

        def matching_code() -> str:
            if not totp_auth.validate_totp_code(self.secret, code):
                raise get_mfa_adapter().validation_error("incorrect_code")
            return code

        return check_second_factor(
            self.user, matching_code, refusal="rate_limited", per_client=False
        )
