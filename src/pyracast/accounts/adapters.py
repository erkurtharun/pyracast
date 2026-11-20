import secrets
from datetime import datetime, timedelta
from typing import Any, cast

from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.models import EmailAddress
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.core.internal.ratelimit import RateLimitUsage
from allauth.headless.internal.restkit.response import ErrorResponse
from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse
from django.utils import timezone, translation
from django.utils.translation import gettext_lazy

from pyracast.accounts.account_mail import NOTICES, render_notice
from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.accounts.language import user_language
from pyracast.accounts.login_limits import (
    failure_history,
    finish_failure,
    locked_on_finished_failures,
    reserve_address_failure,
    reserve_client_failure,
)
from pyracast.accounts.models import Language
from pyracast.accounts.security import normalize_client_ip
from pyracast.accounts.social import record_social_refusal
from pyracast.db import violated_constraint
from pyracast.notifications.intents import enqueue_lock_notice, enqueue_mail
from pyracast.notifications.models import MAIL_CONTEXT_KEYS, OutboundMailKind

NOTICE_LIFETIME = timedelta(days=1)
TOTP_ACTIVATED = "mfa/email/totp_activated"
RECOVERY_CODES_GENERATED = "mfa/email/recovery_codes_generated"
WRITE_NOTICES = frozenset({TOTP_ACTIVATED, RECOVERY_CODES_GENERATED})
CODE_TIMEOUTS = {
    "account/email/email_confirmation": "ACCOUNT_EMAIL_VERIFICATION_BY_CODE_TIMEOUT",
    "account/email/email_confirmation_signup": "ACCOUNT_EMAIL_VERIFICATION_BY_CODE_TIMEOUT",
    "account/email/password_reset_code": "ACCOUNT_PASSWORD_RESET_BY_CODE_TIMEOUT",
    "account/email/login_code": "ACCOUNT_LOGIN_BY_CODE_TIMEOUT",
}


def mail_language() -> Language:
    language = translation.get_language()
    return Language(language) if language in Language.values else Language.EN


def mail_lifetime(template_prefix: str) -> timedelta:
    timeout = CODE_TIMEOUTS.get(template_prefix)
    return timedelta(seconds=getattr(settings, timeout)) if timeout else NOTICE_LIFETIME


def stored_context(context: dict[str, Any]) -> dict[str, str]:
    return {
        key: value.isoformat() if isinstance(value, datetime) else str(value)
        for key in MAIL_CONTEXT_KEYS
        if (value := context.get(key)) is not None
    }


LOGIN_LIMITED_MESSAGE = gettext_lazy(
    "Too many sign-in attempts, or a verification code was sent recently. "
    "Try again in a few minutes."
)


class AccountAdapter(DefaultAccountAdapter):
    _client_failure: RateLimitUsage | None = None
    _login_failed_rl_usage: RateLimitUsage | None = None

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

    def pre_authenticate(self, request: HttpRequest, **credentials: Any) -> None:
        # Replaces allauth's consume of `login_failed`, which keeps the 10-minute hit when the
        # daily rate refuses; allauth's success path rolls `_login_failed_rl_usage` back.
        self._client_failure = self._login_failed_rl_usage = None
        key = self._get_login_attempts_cache_key(request, **credentials)
        self._client_failure = reserve_client_failure(request)
        if self._client_failure is not None:
            self._login_failed_rl_usage = reserve_address_failure(request, key)
            if self._login_failed_rl_usage is None:
                self._client_failure.rollback()
                self._client_failure = None
        if self._login_failed_rl_usage is None:
            address = credentials.get("email")
            record_security_event(SecurityEvent.LOGIN_LOCKED, request=request, address=address)
            history = failure_history(request, key)
            if history is not None and locked_on_finished_failures(history):
                self._ensure_lock_notice(credentials)
            raise self.validation_error("too_many_login_attempts")

    def authenticate(self, request: HttpRequest, **credentials: Any) -> User | None:
        try:
            user = cast(User | None, super().authenticate(request, **credentials))
        except BaseException:
            # Only an accepted or a wrong attempt is decided; a check that ends otherwise is no
            # failure, so its reservations are released before the error leaves.
            for reservation in (self._client_failure, self._login_failed_rl_usage):
                if reservation is not None:
                    reservation.rollback()
            self._client_failure = self._login_failed_rl_usage = None
            raise
        if user is not None and self._client_failure is not None:
            self._client_failure.rollback()
        return user

    def pre_login(
        self, request: HttpRequest, user: AbstractBaseUser, **kwargs: Any
    ) -> HttpResponse | None:
        # allauth refuses an inactive account here. The authentication backend refuses an
        # inactive password sign-in earlier, so only a provider sign-in arrives inactive.
        sociallogin = (kwargs.get("signal_kwargs") or {}).get("sociallogin")
        if not user.is_active and sociallogin is not None:
            record_social_refusal(
                request, sociallogin.account.provider, "inactive", cast(User, user)
            )
        return super().pre_login(request, user, **kwargs)

    def get_login_stages(self) -> list[str]:
        return [
            "pyracast.accounts.signup.EmailVerificationStage"
            if stage == "allauth.account.stages.EmailVerificationStage"
            else stage
            for stage in super().get_login_stages()
        ]

    def generate_unique_username(self, txts: list[str | None], regex: str | None = None) -> str:
        # A random part before allauth's uniqueness check keeps concurrent sign-ups with one
        # mailbox name from choosing one username; allauth still avoids any taken name.
        token = secrets.token_hex(8)
        candidates: list[str | None] = []
        for text in txts:
            name, at, domain = (text or "").partition("@")
            candidates.append(f"{name}-{token}{at}{domain}" if name else text)
        return super().generate_unique_username(candidates, regex)

    def get_client_ip(self, request: HttpRequest) -> str:
        try:
            return normalize_client_ip(super().get_client_ip(request))
        except ValueError:
            raise PermissionDenied("Unable to determine client IP address") from None

    def set_password(self, user: AbstractBaseUser, password: str) -> None:
        user.set_password(password)
        # A full save would insert an account deleted meanwhile again; this one raises NotUpdated.
        user.save(update_fields=["password"])

    @transaction.atomic
    def confirm_email(self, request: HttpRequest, email_address: EmailAddress) -> bool:
        User.objects.select_for_update().get(pk=email_address.user_id)
        try:
            with transaction.atomic():
                confirmed = super().confirm_email(request, email_address)
        except IntegrityError as error:
            if violated_constraint(error) != "unique_verified_email":
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
        # allauth sends these from the commit of their write, outside its transaction; the
        # write's view stores them in that transaction instead (mfa_views).
        if template_prefix in WRITE_NOTICES:
            return
        self.notify(template_prefix, user, context, email)

    def notify(
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
        # The intent is stored in the request's transaction and sent by the mailer, so the
        # answer never waits for or reveals delivery.
        user = context.get("user")
        preference = user_language(user) if isinstance(user, User) else None
        now = timezone.now()
        enqueue_mail(
            OutboundMailKind(template_prefix),
            email,
            language=preference or mail_language(),
            owner_language=not isinstance(user, User),
            context=stored_context(context),
            code=str(context.get("code", "")),
            expires_at=now + mail_lifetime(template_prefix),
            now=now,
        )

    def authentication_failed(self, request: HttpRequest, **credentials: Any) -> None:
        history = finish_failure(self._login_failed_rl_usage or RateLimitUsage(usage=[]))
        # A finished failure counts toward the lock whatever happens next, so its reservations
        # are never released.
        self._client_failure = self._login_failed_rl_usage = None
        if history is not None and locked_on_finished_failures(history):
            self._ensure_lock_notice(credentials)

    def _ensure_lock_notice(self, credentials: dict[str, Any]) -> None:
        if address := str(credentials.get("email") or "").strip().lower():
            enqueue_lock_notice(address, language=mail_language(), lifetime=NOTICE_LIFETIME)
