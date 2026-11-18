from typing import Any, cast

from allauth.headless.account.inputs import SignupInput
from allauth.headless.account.views import SignupView
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import (
    UserAttributeSimilarityValidator,
    get_default_password_validators,
    validate_password,
)
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest

from pyracast.accounts.inputs import PasswordInput


class CompanySignupInput(PasswordInput, SignupInput):
    def clean(self) -> dict[str, Any]:
        data = cast(dict[str, Any], super().clean())
        password, email = self.cleaned_data.get("password"), self.cleaned_data.get("email")
        if password and email:
            # allauth validates a sign-up password without a user, which skips the similarity rule.
            similarity = [
                validator
                for validator in get_default_password_validators()
                if isinstance(validator, UserAttributeSimilarityValidator)
            ]
            account = User(email=email, username=email.partition("@")[0])
            try:
                validate_password(password, account, password_validators=similarity)
            except ValidationError as error:
                self.add_error("password", error)
        return data

    def save(self, request: HttpRequest) -> User:
        with transaction.atomic():
            return cast(User, super().save(request))


class CompanySignupView(SignupView):
    input_class = {"POST": CompanySignupInput}  # noqa: RUF012 -- Allauth's view contract.
