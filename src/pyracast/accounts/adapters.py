import logging
from typing import Any, cast

from allauth.account import app_settings as account_settings
from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.models import EmailAddress
from allauth.core import context as allauth_context
from allauth.core import ratelimit
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.core.internal.ratelimit import RateLimitUsage
from allauth.headless.internal.restkit.response import ErrorResponse
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.template.loader import render_to_string
from django.utils import translation
from django.utils.translation import gettext_lazy

from pyracast.accounts.audit import record_security_event
from pyracast.accounts.authentication import email_identity
from pyracast.accounts.language import user_language
from pyracast.accounts.login_limits import claim_daily_lock_notice, reserve_client_failure
from pyracast.accounts.security import normalize_client_ip
from pyracast.mail_delivery import NotSentError

logger = logging.getLogger(__name__)

LOGIN_LIMITED_MESSAGE = gettext_lazy(
    "Too many sign-in attempts, or a verification code was sent recently. "
    "Try again in a few minutes."
)
ANONYMOUS_ACCOUNT_MAIL = "anonymous_account_mail"
ANONYMOUS_ACCOUNT_TEMPLATES = frozenset(
    {"account/email/unknown_account", "account/email/account_already_exists"}
)


class AccountAdapter(DefaultAccountAdapter):
    _client_failure: RateLimitUsage | None = None

    def validation_error(self, code: str, *args: Any) -> ValidationError:
        # allauth also answers a sign-in within the confirm_email window with this code.
        if code == "too_many_login_attempts":
            return ValidationError(LOGIN_LIMITED_MESSAGE, code=code)
        return super().validation_error(code, *args)

    def render_mail(
        self,
        template_prefix: str,
        email: str | list[str],
        context: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> EmailMultiAlternatives:
        if template_prefix.startswith("account/email/email_confirmation"):
            timeout = settings.ACCOUNT_EMAIL_VERIFICATION_BY_CODE_TIMEOUT
        elif template_prefix == "account/email/login_code":
            timeout = account_settings.LOGIN_BY_CODE_TIMEOUT
        else:
            timeout = settings.ACCOUNT_PASSWORD_RESET_BY_CODE_TIMEOUT
        context = {**context, "code_timeout_minutes": timeout / 60}
        message = cast(
            EmailMultiAlternatives,
            super().render_mail(template_prefix, email, context, headers=headers),
        )
        if not message.alternatives:
            message.attach_alternative(
                render_to_string(
                    "mail/transactional.html",
                    {"mail_title": message.subject, "mail_body": message.body},
                ),
                "text/html",
            )
        return message

    def pre_authenticate(self, request: HttpRequest, **credentials: Any) -> None:
        self._client_failure = reserve_client_failure(request)
        if self._client_failure is None:
            record_security_event("login_locked", request=request)
            raise self.validation_error("too_many_login_attempts")
        try:
            super().pre_authenticate(request, **credentials)
        except ValidationError:
            self._client_failure.rollback()
            record_security_event("login_locked", request=request)
            key = self._get_login_attempts_cache_key(request, **credentials)
            if claim_daily_lock_notice(request, key):
                self.send_login_locked_notice(credentials.get("email", ""))
            raise

    def authenticate(self, request: HttpRequest, **credentials: Any) -> User | None:
        user = cast(User | None, super().authenticate(request, **credentials))
        if user is not None and self._client_failure is not None:
            self._client_failure.rollback()
        return user

    def send_login_locked_notice(self, email: str) -> None:
        user = email_identity(email)
        address = EmailAddress.objects.get_primary_email(user) if user is not None else None
        if address:
            self.send_mail("account/email/login_locked", address, {"user": user})

    def get_client_ip(self, request: HttpRequest) -> str:
        try:
            return normalize_client_ip(super().get_client_ip(request))
        except ValueError:
            raise PermissionDenied("Unable to determine client IP address") from None

    @transaction.atomic
    def confirm_email(self, request: HttpRequest, email_address: EmailAddress) -> bool:
        User.objects.select_for_update().get(pk=email_address.user_id)
        try:
            with transaction.atomic():
                confirmed = super().confirm_email(request, email_address)
        except IntegrityError as error:
            if (
                getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
                != "unique_verified_email"
            ):
                raise
            confirmed = False
        if not confirmed:
            raise ImmediateHttpResponse(
                ErrorResponse(request, exception=self.validation_error("email_taken"))
            )
        return True

    def send_mail(self, template_prefix: str, email: str, context: dict[str, Any]) -> None:
        if template_prefix in ANONYMOUS_ACCOUNT_TEMPLATES and not ratelimit.consume(
            allauth_context.request, action=ANONYMOUS_ACCOUNT_MAIL, key="all"
        ):
            logger.warning("account_mail_skipped template=%s reason=budget", template_prefix)
            return
        user = context.get("user")
        preference = user_language(user) if isinstance(user, User) else None
        language = preference or translation.get_language()

        def send() -> None:
            with translation.override(language):
                try:
                    super(AccountAdapter, self).send_mail(template_prefix, email, context)
                except Exception as error:
                    # Transport errors can carry the recipient address, so neither the message
                    # nor the traceback is logged.
                    logger.error(
                        "account_mail_failed template=%s user_id=%s error_type=%s error_code=%s",
                        template_prefix,
                        user.pk if isinstance(user, User) else None,
                        type(error).__name__,
                        error.code if isinstance(error, NotSentError) else None,
                    )

        transaction.on_commit(send)
