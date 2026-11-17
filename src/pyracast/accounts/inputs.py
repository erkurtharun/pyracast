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
from django import forms
from django.contrib.auth.models import User
from django.core.validators import MaxLengthValidator

from pyracast.accounts.audit import record_security_event
from pyracast.accounts.authentication import email_identity


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
