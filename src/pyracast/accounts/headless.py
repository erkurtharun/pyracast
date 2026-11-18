import json
from typing import Any, cast

from allauth.account.internal.flows.code_verification import AbstractCodeVerificationProcess
from allauth.account.internal.flows.email_verification import login_on_verification
from allauth.account.internal.flows.email_verification_by_code import EmailVerificationProcess
from allauth.account.internal.flows.password_reset_by_code import (
    PasswordResetVerificationProcess,
)
from allauth.account.internal.flows.reauthentication import raise_if_reauthentication_required
from allauth.account.internal.userkit import str_to_user_id
from allauth.account.models import EmailAddress
from allauth.headless.account import views
from allauth.headless.base.response import AuthenticationResponse, ConflictResponse
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, ObjectNotUpdated
from django.db import IntegrityError, OperationalError, transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from psycopg.errors import DeadlockDetected

from pyracast.accounts import inputs
from pyracast.accounts.adapters import violated_constraint

COMPLETION_FAILURES = (ObjectDoesNotExist, ObjectNotUpdated, IntegrityError, OperationalError)
# The second name is the one Django generated for allauth's unique (user, email).
STALE_CONSTRAINTS = {"unique_verified_email", "account_emailaddress_user_id_email_987c8728_uniq"}


def failed_completion(error: Exception) -> bool:
    if isinstance(error, IntegrityError):
        return violated_constraint(error) in STALE_CONSTRAINTS
    # Only a deadlock answers 409; a lock timeout reaches handler500, which answers 503 busy.
    if isinstance(error, OperationalError):
        return isinstance(error.__cause__, DeadlockDetected)
    return True


def owner(process: AbstractCodeVerificationProcess) -> Any:
    user_id = process.state.get("user_id")
    return None if user_id is None else str_to_user_id(user_id)


def account_gone(process: AbstractCodeVerificationProcess) -> bool:
    pk = owner(process)
    return pk is not None and not User.objects.filter(pk=pk).exists()


def settled(process: EmailVerificationProcess) -> bool:
    return (
        account_gone(process)
        or EmailAddress.objects.filter(
            user_id=owner(process), email__iexact=process.email, verified=True
        ).exists()
    )


def discard_settled_verification(request: HttpRequest) -> None:
    process = EmailVerificationProcess.resume(request)
    if process is not None and settled(process):
        process.abort()


def claim(address: EmailAddress) -> None:
    # A conditional write of the row completion saves next: it matches only while the row is still
    # unverified and holds the cached owner and email, the fields completion writes back from the
    # cached object, and keeps the row unchanged by others until completion commits.
    claimed = EmailAddress.objects.filter(
        pk=address.pk, user_id=address.user_id, email=address.email, verified=False
    ).update(verified=False)
    if not claimed:
        raise EmailAddress.DoesNotExist


def claim_verification(process: EmailVerificationProcess) -> None:
    address = process.email_address
    if address.pk:
        claim(address)
    elif settled(process):
        raise EmailAddress.DoesNotExist


def claim_reset_address(process: PasswordResetVerificationProcess) -> None:
    user = cast(User, process.user)
    try:
        address = EmailAddress.objects.get_for_user(user, process.state["email"])
    except EmailAddress.DoesNotExist:
        addresses = []
    else:
        if not address.verified:
            claim(address)
        addresses = [address]
    # allauth's indirect verification reads the address from this cache and saves the claimed row.
    EmailAddress.objects.fill_cache_for_user(user, addresses)


class LoginView(views.LoginView):
    input_class = inputs.LoginInput

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method == "POST" and request.user.is_authenticated:
            return ConflictResponse(request)
        return super().handle(request, *args, **kwargs)


class RequestPasswordResetView(views.RequestPasswordResetView):
    input_class = inputs.RequestPasswordResetInput


class ResetPasswordView(views.ResetPasswordView):
    input_class = inputs.ResetPasswordInput

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        process = PasswordResetVerificationProcess.resume(request)
        if process is not None and account_gone(process):
            process.abort()
        return super().handle(request, *args, **kwargs)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        process = cast(PasswordResetVerificationProcess, self.process)
        try:
            with transaction.atomic():
                claim_reset_address(process)
                return super().post(request, *args, **kwargs)
        except COMPLETION_FAILURES as error:
            if not failed_completion(error):
                raise
            process.abort()
            return ConflictResponse(request)


class VerifyEmailView(views.VerifyEmailView):
    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        discard_settled_verification(request)
        return super().handle(request, *args, **kwargs)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        process = cast(EmailVerificationProcess, self.process)
        try:
            with transaction.atomic():
                claim_verification(process)
                email_address = process.finish()
        except COMPLETION_FAILURES as error:
            if not failed_completion(error):
                raise
            process.abort()
            return ConflictResponse(request)
        response = login_on_verification(request, email_address) if self.stage else None
        return AuthenticationResponse.from_response(request, response)


class ResendEmailVerificationCodeView(views.ResendEmailVerificationCodeView):
    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        discard_settled_verification(request)
        return super().handle(request, *args, **kwargs)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        with transaction.atomic():
            response = super().post(request, *args, **kwargs)
            process = EmailVerificationProcess.resume(request)
            if process is not None and settled(process):
                # Rolling back drops the code mail, which waits for the commit.
                transaction.set_rollback(True)
                process.abort()
                return ConflictResponse(request)
        return response


class ChangePasswordView(views.ChangePasswordView):
    input_class = inputs.ChangePasswordInput

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        response: HttpResponse = super().post(request, *args, **kwargs)
        payload = json.loads(response.content)
        payload.setdefault("meta", {})["password_changed"] = True
        response.content = json.dumps(payload)
        return response


class ReauthenticateView(views.ReauthenticateView):
    input_class = inputs.ReauthenticateInput


class ManageEmailView(views.ManageEmailView):
    input_class = views.ManageEmailView.input_class | {"PUT": inputs.ResendEmailVerificationInput}

    def patch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if self.input.cleaned_data["email"].primary:
            raise_if_reauthentication_required(request, app="allauth.account")
            return self._respond_email_list()
        return super().patch(request, *args, **kwargs)
