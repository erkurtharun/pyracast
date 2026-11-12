from typing import cast

from allauth.headless.account.inputs import SignupInput
from allauth.headless.account.views import SignupView
from allauth.headless.internal.restkit.inputs import Input
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest

from pyracast.accounts.inputs import PasswordInput


class CompanySignupInput(PasswordInput, SignupInput):
    @transaction.atomic
    def save(self, request: HttpRequest) -> User:
        return cast(User, super().save(request))


class CompanySignupView(SignupView):
    input_class: dict[str, type[Input]] = {"POST": CompanySignupInput}  # noqa: RUF012 -- Allauth's view contract.
