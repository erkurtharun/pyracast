import time
from collections.abc import Callable
from http import HTTPStatus
from typing import Any, cast

from allauth.account import stages
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
from django.urls import reverse

from pyracast.accounts.addresses import plain_address
from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.accounts.inputs import PasswordInput
from pyracast.accounts.processes import StartingView, lock_account


class CompanySignupInput(PasswordInput, SignupInput):
    def clean_email(self) -> str:
        plain_address(self.cleaned_data["email"])
        email: str = super().clean_email()
        return email

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
            user = cast(User, super().save(request))
            lock_account(user.pk)
            return user


class CompanySignupView(StartingView, SignupView):
    input_class = {"POST": CompanySignupInput}  # noqa: RUF012 -- Allauth's view contract.


class EmailVerificationStage(stages.EmailVerificationStage):
    def handle(self) -> tuple[HttpResponse | None, bool]:
        handle = cast(Callable[[], tuple[HttpResponse | None, bool]], super().handle)
        try:
            return handle()
        except ImmediateHttpResponse as refusal:
            # Both kinds of password sign-up answer this refusal like a sent mail, so it is
            # recorded where it happens.
            if (
                self.login.signup
                and "sociallogin" not in (self.login.signal_kwargs or {})
                and refusal.response.status_code == HTTPStatus.TOO_MANY_REQUESTS
            ):
                record_security_event(
                    SecurityEvent.AUTHENTICATION_REQUEST_THROTTLED, request=self.request
                )
            raise


class SignupMinimumAnswerTime:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        started = (
            time.monotonic()
            if request.method == "POST" and request.path_info == reverse("password_signup")
            else None
        )
        response = self.get_response(request)
        if started is not None and response.status_code != HTTPStatus.TOO_MANY_REQUESTS:
            elapsed = time.monotonic() - started
            remaining = settings.PYRACAST_SIGNUP_MINIMUM_ANSWER_SECONDS - elapsed
            if remaining > 0:
                time.sleep(remaining)
        return response
