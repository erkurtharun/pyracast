from typing import Any, cast

from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.models import EmailAddress
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.headless.internal.restkit.response import ErrorResponse
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.template.loader import render_to_string
from django.utils import translation

from pyracast.accounts.language import user_language
from pyracast.accounts.security import normalize_client_ip


class AccountAdapter(DefaultAccountAdapter):
    def render_mail(
        self,
        template_prefix: str,
        email: str | list[str],
        context: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> EmailMultiAlternatives:
        timeout = (
            settings.ACCOUNT_EMAIL_VERIFICATION_BY_CODE_TIMEOUT
            if template_prefix.startswith("account/email/email_confirmation")
            else settings.ACCOUNT_PASSWORD_RESET_BY_CODE_TIMEOUT
        )
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

    def send_mail(self, template_prefix: str, email: str, context: dict[str, Any]) -> None:
        user = context.get("user")
        preference = user_language(user) if isinstance(user, User) else None
        language = preference or translation.get_language()

        def send() -> None:
            with translation.override(language):
                super(AccountAdapter, self).send_mail(template_prefix, email, context)

        transaction.on_commit(send)
