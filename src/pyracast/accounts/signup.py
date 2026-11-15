from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.headless.account.inputs import SignupInput
from allauth.headless.account.views import SignupView
from allauth.headless.internal.restkit.response import ErrorResponse
from django import forms
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils.translation import gettext as _

from pyracast.accounts.inputs import PasswordInput


@contextmanager
def workspace_signup_transaction(request: HttpRequest, signup_input: forms.Form) -> Iterator[None]:
    with transaction.atomic():
        try:
            with transaction.atomic():
                yield
        except IntegrityError as error:
            if (
                getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
                != "unique_workspace_name"
            ):
                raise
            signup_input.add_error("company_name", _("This workspace name is already in use."))
            raise ImmediateHttpResponse(ErrorResponse(request, input=signup_input)) from error


class CompanySignupInput(PasswordInput, SignupInput):
    def save(self, request: HttpRequest) -> User:
        with workspace_signup_transaction(request, self):
            return cast(User, super().save(request))


class CompanySignupView(SignupView):
    input_class = {"POST": CompanySignupInput}  # noqa: RUF012 -- Allauth's view contract.
