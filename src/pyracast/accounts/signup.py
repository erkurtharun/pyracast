import time
from http import HTTPStatus
from typing import Any, cast

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.headless.account.inputs import SignupInput
from allauth.headless.account.views import SignupView
from allauth.headless.base.response import AuthenticationResponse
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import (
    UserAttributeSimilarityValidator,
    get_default_password_validators,
    validate_password,
)
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase

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

    def try_save(self, request: HttpRequest) -> tuple[User | None, HttpResponse | None]:
        if not self.account_already_exists:
            return cast(tuple[User | None, HttpResponse | None], super().try_save(request))
        # An address with an account gets no new account but the same password hashing, so
        # both answers take as long.
        make_password(self.cleaned_data.get("password"))
        try:
            return cast(tuple[User | None, HttpResponse | None], super().try_save(request))
        except ImmediateHttpResponse:
            # allauth's `SignupView.post` drops a response raised by `complete_signup` for a new
            # address and answers the pending sign-up; an address with an account answers alike.
            return None, AuthenticationResponse(request)

    def save(self, request: HttpRequest) -> User:
        with transaction.atomic():
            return cast(User, super().save(request))


class CompanySignupView(SignupView):
    input_class = {"POST": CompanySignupInput}  # noqa: RUF012 -- Allauth's view contract.

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method != "POST":
            return super().dispatch(request, *args, **kwargs)
        started = time.monotonic()
        try:
            response = super().dispatch(request, *args, **kwargs)
        except ImmediateHttpResponse as error:
            response = error.response
        except Exception:
            _wait_for_minimum_answer_time(started)
            raise
        if response.status_code != HTTPStatus.TOO_MANY_REQUESTS:
            _wait_for_minimum_answer_time(started)
        return response


def _wait_for_minimum_answer_time(started: float) -> None:
    remaining = settings.PYRACAST_SIGNUP_MINIMUM_ANSWER_SECONDS - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)
