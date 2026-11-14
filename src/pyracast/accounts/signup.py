from typing import cast

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.headless.account.inputs import SignupInput
from allauth.headless.account.views import SignupView
from allauth.headless.internal.restkit.response import ErrorResponse
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils.translation import gettext as _

from pyracast.accounts.inputs import PasswordInput


class CompanySignupInput(PasswordInput, SignupInput):
    @transaction.atomic
    def save(self, request: HttpRequest) -> User:
        try:
            with transaction.atomic():
                return cast(User, super().save(request))
        except IntegrityError as error:
            if (
                getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
                != "unique_workspace_name"
            ):
                raise
            self.add_error("company_name", _("This workspace name is already in use."))
            raise ImmediateHttpResponse(ErrorResponse(request, input=self)) from error


class CompanySignupView(SignupView):
    input_class = {"POST": CompanySignupInput}  # noqa: RUF012 -- Allauth's view contract.
