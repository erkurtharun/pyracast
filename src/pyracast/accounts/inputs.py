from typing import Any, cast

from allauth.account.adapter import get_adapter
from allauth.core import context
from allauth.headless.account.inputs import (
    AddEmailInput as AllauthAddEmailInput,
)
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
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.http import HttpRequest

from pyracast.accounts.addresses import plain_address
from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.accounts.authentication import recovery_identity
from pyracast.accounts.login_limits import check_second_factor, spent, throttled


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


def sign_in_password(password: str) -> str:
    # Raises ValidationError for a password the sign-in form would refuse to read.
    cleaned: str = LoginInput().fields["password"].clean(password)
    return cleaned


class AddEmailInput(AllauthAddEmailInput):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # allauth accepts the 320 characters of Django's email field; account addresses hold 254.
        field = cast(forms.EmailField, self.fields["email"])
        field.max_length = 254
        field.validators = [
            validator
            for validator in field.validators
            if not isinstance(validator, MaxLengthValidator)
        ] + [MaxLengthValidator(254)]

    def clean_email(self) -> str:
        plain_address(self.cleaned_data["email"])
        email: str = super().clean_email()
        return email


class RequestPasswordResetInput(AllauthRequestPasswordResetInput):
    users: list[User]

    def clean_email(self) -> str:
        email: str = super().clean_email()
        if spent(context.request, "reset_password", key=email.lower()):
            raise throttled(context.request)
        user = recovery_identity(email)
        self.users = [] if user is None else [user]
        return email

    def save(self, request: HttpRequest, **kwargs: Any) -> str:
        email: str = super().save(request, **kwargs)
        record_security_event(
            SecurityEvent.RESET_REQUESTED,
            request=request,
            user_id=self.users[0].pk if self.users else None,
            address=email,
        )
        return email


class ResetPasswordInput(PasswordInput, AllauthResetPasswordInput):
    pass


class ChangePasswordInput(PasswordInput, AllauthChangePasswordInput):
    user: User

    def clean_current_password(self) -> str:
        current_password: str = self.cleaned_data["current_password"]
        if current_password and not self.user.check_password(current_password):
            record_security_event(
                SecurityEvent.CURRENT_CREDENTIAL_REFUSED,
                request=context.request,
                user_id=self.user.pk,
            )
            raise get_adapter().validation_error("enter_current_password")
        return current_password


class ReauthenticateInput(PasswordInput, AllauthReauthenticateInput):
    user: User

    def clean_password(self) -> str:
        try:
            password: str = super().clean_password()
        except ValidationError:
            record_security_event(
                SecurityEvent.REAUTHENTICATION_FAILED,
                request=context.request,
                user_id=self.user.pk,
                method="password",
            )
            raise
        return password


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
