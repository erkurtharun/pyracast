from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.headless.account.inputs import SignupInput
from allauth.headless.account.views import SignupView
from allauth.headless.internal.restkit.response import ErrorResponse
from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import (
    UserAttributeSimilarityValidator,
    get_default_password_validators,
    validate_password,
)
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils.translation import gettext as _

from pyracast.accounts.inputs import PasswordInput
from pyracast.db import violated_constraint


@contextmanager
def workspace_signup_transaction(request: HttpRequest, signup_input: forms.Form) -> Iterator[None]:
    with transaction.atomic():
        try:
            with transaction.atomic():
                yield
        except IntegrityError as error:
            if violated_constraint(error) != "unique_workspace_name":
                raise
            signup_input.add_error("company_name", _("This workspace name is already in use."))
            raise ImmediateHttpResponse(ErrorResponse(request, input=signup_input)) from error


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
        with workspace_signup_transaction(request, self):
            return cast(User, super().save(request))


class CompanySignupView(SignupView):
    input_class = {"POST": CompanySignupInput}  # noqa: RUF012 -- Allauth's view contract.
