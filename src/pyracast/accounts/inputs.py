from typing import Any

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
from django.core.validators import MaxLengthValidator

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
    def clean_email(self) -> str:
        email: str = super().clean_email()
        user = email_identity(email, for_recovery=True)
        self.users = [user] if user is not None and user.is_active else []
        return email


class ResetPasswordInput(PasswordInput, AllauthResetPasswordInput):
    pass


class ChangePasswordInput(PasswordInput, AllauthChangePasswordInput):
    pass


class ReauthenticateInput(PasswordInput, AllauthReauthenticateInput):
    pass
