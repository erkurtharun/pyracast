import json
from typing import Any, cast

from allauth.account.internal.flows.email_verification import login_on_verification
from allauth.account.internal.flows.email_verification_by_code import (
    EMAIL_VERIFICATION_CODE_SESSION_KEY,
    EmailVerificationProcess,
)
from allauth.account.internal.flows.password_reset_by_code import (
    PASSWORD_RESET_VERIFICATION_SESSION_KEY,
    PasswordResetVerificationProcess,
)
from allauth.account.internal.flows.reauthenticate_by_code import (
    REAUTHENTICATION_CODE_SESSION_KEY,
)
from allauth.account.internal.stagekit import LOGIN_SESSION_KEY
from allauth.account.models import EmailAddress
from allauth.core import ratelimit
from allauth.headless.account import views
from allauth.headless.base.response import AuthenticationResponse, ConflictResponse
from allauth.headless.internal.restkit.response import ErrorResponse
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, ObjectNotUpdated
from django.db import IntegrityError, OperationalError, transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from psycopg.errors import DeadlockDetected

from pyracast.accounts import inputs
from pyracast.accounts.processes import (
    AuthorityChangedError,
    StartingView,
    continuing,
    lock_account,
    require_authority,
)
from pyracast.db import violated_constraint

COMPLETION_FAILURES = (
    AuthorityChangedError,
    ObjectDoesNotExist,
    ObjectNotUpdated,
    IntegrityError,
    OperationalError,
)
# The second name is the one Django generated for allauth's unique (user, email).
STALE_CONSTRAINTS = {"unique_verified_email", "account_emailaddress_user_id_email_987c8728_uniq"}


def failed_completion(error: Exception) -> bool:
    if isinstance(error, IntegrityError):
        return violated_constraint(error) in STALE_CONSTRAINTS
    # Only a deadlock answers 409; a lock timeout reaches handler500, which answers 503 busy.
    if isinstance(error, OperationalError):
        return isinstance(error.__cause__, DeadlockDetected)
    return True


def claim(address: EmailAddress) -> None:
    # A conditional write of the row completion saves next: it matches only while the row is still
    # unverified and holds the cached owner and email, the fields completion writes back from the
    # cached object, and keeps the row unchanged by others until completion commits.
    claimed = EmailAddress.objects.filter(
        pk=address.pk, user_id=address.user_id, email=address.email, verified=False
    ).update(verified=False)
    if not claimed:
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


class LoginView(StartingView, views.LoginView):
    input_class = inputs.LoginInput

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method == "POST" and request.user.is_authenticated:
            return ConflictResponse(request)
        return super().handle(request, *args, **kwargs)


class RequestPasswordResetView(StartingView, views.RequestPasswordResetView):
    input_class = inputs.RequestPasswordResetInput


class RequestReauthenticationCodeView(StartingView, views.RequestReauthenticationCodeView):
    def handle_input(self, data: Any) -> ErrorResponse | None:
        lock_account(self.request.user.pk)
        return super().handle_input(data)


class ResetPasswordView(views.ResetPasswordView):
    input_class = inputs.ResetPasswordInput

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        process = cast(PasswordResetVerificationProcess, self.process)
        try:
            with transaction.atomic():
                claim_reset_address(process)
                require_authority(request, PASSWORD_RESET_VERIFICATION_SESSION_KEY)
                return super().post(request, *args, **kwargs)
        except COMPLETION_FAILURES as error:
            if not failed_completion(error):
                raise
            process.abort()
            return ConflictResponse(request)


class VerifyEmailView(views.VerifyEmailView):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        process = cast(EmailVerificationProcess, self.process)
        try:
            with transaction.atomic():
                if process.email_address.pk:
                    claim(process.email_address)
                require_authority(request, EMAIL_VERIFICATION_CODE_SESSION_KEY)
                if self.stage:
                    require_authority(request, LOGIN_SESSION_KEY)
                email_address = process.finish()
                response = None
                if self.stage:
                    # The sign-in rests on the state checked above, so it commits under its locks.
                    response = login_on_verification(request, email_address)
                    continuing(request, LOGIN_SESSION_KEY)
        except COMPLETION_FAILURES as error:
            if not failed_completion(error):
                raise
            process.abort()
            return ConflictResponse(request)
        return AuthenticationResponse.from_response(request, response)


class ResendEmailVerificationCodeView(views.ResendEmailVerificationCodeView):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        with transaction.atomic():
            response = super().post(request, *args, **kwargs)
            try:
                require_authority(request, EMAIL_VERIFICATION_CODE_SESSION_KEY)
            except AuthorityChangedError:
                # Rolling back drops the code mail, which waits for the commit.
                transaction.set_rollback(True)
                if (process := EmailVerificationProcess.resume(request)) is not None:
                    process.abort()
                return ConflictResponse(request)
        return response


class ConfirmReauthenticationCodeView(views.ConfirmReauthenticationCodeView):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            with transaction.atomic():
                require_authority(request, REAUTHENTICATION_CODE_SESSION_KEY)
                return super().post(request, *args, **kwargs)
        except AuthorityChangedError:
            self.process.abort()
            return ConflictResponse(request)


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


class ManageEmailView(StartingView, views.ManageEmailView):
    def handle_input(self, data: Any) -> ErrorResponse | None:
        if self.request.method == "POST":
            lock_account(self.user.pk)
        return super().handle_input(data)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        # Changing the address a sign-up verifies continues its code and its sign-in step.
        stage = self.verification_stage_process
        if stage is not None:
            try:
                require_authority(request, EMAIL_VERIFICATION_CODE_SESSION_KEY)
                require_authority(request, LOGIN_SESSION_KEY)
            except AuthorityChangedError:
                stage.abort()
                return ConflictResponse(request)
        limited = ratelimit.consume_or_429(request, action="request_email_change", user=self.user)
        if limited is not None:
            return limited
        response = super().post(request, *args, **kwargs)
        if stage is not None:
            continuing(request, EMAIL_VERIFICATION_CODE_SESSION_KEY, LOGIN_SESSION_KEY)
        return response
