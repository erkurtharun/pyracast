import logging
from typing import Any, cast

from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.models import EmailAddress
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.headless.internal.restkit.response import ErrorResponse
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils import translation
from django.utils.translation import gettext_lazy

from pyracast.accounts.account_mail import NOTICES, render_notice
from pyracast.accounts.language import user_language
from pyracast.accounts.security import normalize_client_ip
from pyracast.mail.errors import NotSentError
from pyracast.mail.send import send

logger = logging.getLogger(__name__)
CODE_TIMEOUTS = {
    "account/email/email_confirmation": "ACCOUNT_EMAIL_VERIFICATION_BY_CODE_TIMEOUT",
    "account/email/email_confirmation_signup": "ACCOUNT_EMAIL_VERIFICATION_BY_CODE_TIMEOUT",
    "account/email/password_reset_code": "ACCOUNT_PASSWORD_RESET_BY_CODE_TIMEOUT",
    "account/email/login_code": "ACCOUNT_LOGIN_BY_CODE_TIMEOUT",
}

LOGIN_LIMITED_MESSAGE = gettext_lazy(
    "Too many sign-in attempts, or a verification code was sent recently. "
    "Try again in a few minutes."
)


class AccountAdapter(DefaultAccountAdapter):
    error_messages = DefaultAccountAdapter.error_messages | {
        "email_already_verified": gettext_lazy("This email address is already verified.")
    }

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
        headers = {**(headers or {}), "X-Pyracast-Mail": template_prefix}
        if (notice := NOTICES.get(template_prefix)) is not None:
            subject, body, html = render_notice(notice, context)
            message = EmailMultiAlternatives(
                subject,
                body,
                self.get_from_email(),
                [email] if isinstance(email, str) else email,
                headers=headers,
            )
            message.attach_alternative(html, "text/html")
            return message
        if (timeout := CODE_TIMEOUTS.get(template_prefix)) is not None:
            context = {**context, "code_timeout_minutes": getattr(settings, timeout) // 60}
        return cast(
            EmailMultiAlternatives,
            super().render_mail(template_prefix, email, context, headers=headers),
        )

    def authenticate(self, request: HttpRequest, **credentials: Any) -> User | None:
        user = cast(User | None, super().authenticate(request, **credentials))
        if (
            user is not None
            and (user.is_staff or user.is_superuser)
            and not Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).exists()
        ):
            return None
        return user

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

    def send_notification_mail(
        self,
        template_prefix: str,
        user: AbstractBaseUser,
        context: dict[str, Any] | None = None,
        email: str | None = None,
    ) -> None:
        super().send_notification_mail(
            template_prefix, user, {**(context or {}), "user": user}, email
        )

    def send_mail(self, template_prefix: str, email: str, context: dict[str, Any]) -> None:
        user = context.get("user")
        if not isinstance(user, User):
            address = (
                EmailAddress.objects.filter(email__iexact=email, verified=True)
                .select_related("user")
                .first()
            )
            user = address.user if address is not None else None
        preference = user_language(user) if isinstance(user, User) else None
        language = preference or translation.get_language()

        def deliver() -> None:
            with translation.override(language):
                try:
                    send(self.render_mail(template_prefix, email, {"email": email, **context}))
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

        transaction.on_commit(deliver)
