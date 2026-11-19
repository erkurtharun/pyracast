from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, NoReturn, cast

from allauth.account import signals as account_signals
from allauth.account.internal.flows import reauthentication
from allauth.account.models import EmailAddress
from allauth.core.exceptions import ImmediateHttpResponse, ReauthenticationRequired
from allauth.headless.adapter import DefaultHeadlessAdapter
from allauth.mfa.models import Authenticator
from allauth.socialaccount import signals as social_signals
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.internal.flows.connect import do_connect
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin
from allauth.socialaccount.providers.base.provider import Provider
from django.conf import settings
from django.contrib.auth import get_user
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponseRedirect

from pyracast.accounts.audit import SecurityEvent, record_security_event, refusal_throttle
from pyracast.accounts.authorization import requires_admin_mfa
from pyracast.accounts.processes import (
    identified_accounts,
    identifying,
    lock_account,
    require_current_proof,
)


def unbind_unproven_identities(*, request: HttpRequest, user: User, **_kwargs: Any) -> None:
    # An account that never signed in may hold an identity bound before its address was proven;
    # recovering the address through a reset leaves the mailbox owner as the only authority.
    if user.last_login is not None:
        return
    for account in SocialAccount.objects.filter(user=user):
        account.delete()
        social_signals.social_account_removed.send(
            sender=SocialAccount, request=request, socialaccount=account
        )


def connect_identity_recovery() -> None:
    account_signals.password_reset.connect(
        unbind_unproven_identities, dispatch_uid="pyracast.unbind_unproven_identities"
    )


def social_callback_url() -> str:
    return f"{settings.PYRACAST_PUBLIC_ORIGIN}/app/?social_callback=1"


class HeadlessAdapter(DefaultHeadlessAdapter):
    def get_frontend_url(self, urlname: str, **kwargs: Any) -> str | None:
        if urlname == "socialaccount_login_error":
            return social_callback_url()
        return super().get_frontend_url(urlname, **kwargs)


def record_social_refusal(
    request: HttpRequest, provider: str, reason: str, user: User | None = None
) -> None:
    if not refusal_throttle.admit((0 if user is None else user.pk, f"social:{provider}", reason)):
        return
    record_security_event(
        SecurityEvent.SOCIAL_LOGIN_REFUSED,
        request=request,
        user_id=None if user is None else user.pk,
        provider=provider,
        reason=reason,
    )


class UnavailableSocialAppError(SocialApp.DoesNotExist, ValueError):
    pass


@contextmanager
def locked_social_user(request: HttpRequest, user_id: int) -> Iterator[User | None]:
    # Connecting and removing an identity are credential writes of the session's own account.
    with transaction.atomic():
        if request.user.pk == user_id:
            require_current_proof(request)
        request.user = get_user(request)
        yield (
            request.user if request.user.is_authenticated and request.user.pk == user_id else None
        )


def lock_identity(provider: str, uid: str) -> None:
    # In the callback, which starts pending processes, the account a provider identity names is
    # locked before allauth looks the identity up; the owner is read again under the lock.
    if not identifying():
        return
    identity = SocialAccount.objects.filter(provider=provider, uid=uid)
    owner = identity.values_list("user_id", flat=True).first()
    while owner is not None:
        lock_account(owner)
        current = identity.values_list("user_id", flat=True).first()
        if current == owner:
            return
        owner = current


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def get_app(
        self, request: HttpRequest, provider: str, client_id: str | None = None
    ) -> SocialApp:
        try:
            return cast(SocialApp, super().get_app(request, provider, client_id=client_id))
        except SocialApp.DoesNotExist:
            # Pending signup deserialization treats ValueError as expired state.
            raise UnavailableSocialAppError from None

    def list_providers(self, request: HttpRequest) -> list[Provider]:
        providers: list[Provider] = super().list_providers(request)
        for provider in providers:
            provider.supports_token_authentication = False
        return providers

    def list_apps(
        self, request: HttpRequest | None, provider: str | None = None, client_id: str | None = None
    ) -> list[SocialApp]:
        # Deployment credentials are the only authority for enabling a provider.
        apps = []
        for name, configuration in settings.SOCIALACCOUNT_PROVIDERS.items():
            if provider and name != provider:
                continue
            credentials = configuration.get("APP")
            if not isinstance(credentials, dict):
                continue
            identifier, secret = credentials.get("client_id"), credentials.get("secret")
            if not identifier or not secret or (client_id and identifier != client_id):
                continue
            apps.append(SocialApp(provider=name, client_id=identifier, secret=secret))
        return apps

    def populate_user(
        self, request: HttpRequest, sociallogin: SocialLogin, data: dict[str, Any]
    ) -> User:
        # Runs before allauth looks the identity up and saves it, so no other profile field is
        # ever stored and the account it names is locked before that lookup reads it.
        lock_identity(sociallogin.account.provider, sociallogin.account.uid)
        user = cast(User, super().populate_user(request, sociallogin, data))
        provider = sociallogin.account.provider
        profile = sociallogin.account.extra_data
        for address in sociallogin.email_addresses:
            authoritative_google_email = provider == "google" and (
                address.email.lower().endswith("@gmail.com") or bool(profile.get("hd"))
            )
            address.verified = address.verified and authoritative_google_email
        sociallogin.account.extra_data = {
            "email": profile.get("email")
            or profile.get("mail")
            or profile.get("userPrincipalName")
            or ""
        }
        return user

    def pre_social_login(self, request: HttpRequest, sociallogin: SocialLogin) -> None:
        provider = sociallogin.account.provider
        sociallogin.token = None
        user = cast(User, sociallogin.user) if sociallogin.is_existing else None
        if provider not in settings.SOCIALACCOUNT_PROVIDERS:
            record_social_refusal(request, provider, "provider_disabled", user)
            raise PermissionDenied("This sign-in provider is unavailable.")
        if sociallogin.state.get("process") == "connect":
            self.connect_account(request, sociallogin)
        if request.user.is_authenticated:
            record_social_refusal(request, provider, "signed_in", request.user)
            raise PermissionDenied("Sign out before signing in with a provider.")
        if user is not None and user.pk not in identified_accounts():
            # The identity was connected after the callback looked for it, so its account was
            # read without its lock.
            record_social_refusal(request, provider, "identity_changed", user)
            raise PermissionDenied("The provider identity changed during the sign-in.")
        if (
            user is not None
            and requires_admin_mfa(user)
            and not Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).exists()
        ):
            record_social_refusal(request, provider, "mfa_not_provisioned", user)
            raise PermissionDenied("Administrator sign-in requires a provisioned authenticator.")

    def on_authentication_error(
        self,
        request: HttpRequest,
        provider: Provider,
        error: str | None = None,
        exception: BaseException | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> None:
        # Refusals raised as PermissionDenied were recorded where they were decided.
        if not isinstance(exception, PermissionDenied):
            record_social_refusal(request, provider.id, "provider_error")
        super().on_authentication_error(
            request, provider, error=error, exception=exception, extra_context=extra_context
        )

    def connect_account(self, request: HttpRequest, sociallogin: SocialLogin) -> None:
        origin = sociallogin.state.get("data") or {}
        user_id = origin.get("user_id")
        if not isinstance(user_id, int):
            user = request.user if request.user.is_authenticated else None
            record_social_refusal(
                request, sociallogin.account.provider, "initiating_user_missing", user
            )
            raise PermissionDenied("Account connection requires an initiating user.")
        try:
            with locked_social_user(request, user_id) as user:
                if user is None or reauthentication.is_reauthentication_required(
                    request, app="allauth.socialaccount"
                ):
                    self.refuse_connection(request, sociallogin)
                try:
                    with transaction.atomic():
                        do_connect(request, sociallogin)
                except IntegrityError as error:
                    diagnostic = getattr(error.__cause__, "diag", None)
                    if (
                        getattr(error.__cause__, "sqlstate", None) != "23505"
                        or getattr(diagnostic, "table_name", None) != SocialAccount._meta.db_table
                    ):
                        raise
                    raise self.validation_error("connected_other") from error
        except ReauthenticationRequired:
            self.refuse_connection(request, sociallogin)
        # Ends allauth's flow here: returning would let it run do_connect again, outside the lock.
        raise ImmediateHttpResponse(
            HttpResponseRedirect(self.get_connect_redirect_url(request, sociallogin.account))
        )

    @staticmethod
    def refuse_connection(request: HttpRequest, sociallogin: SocialLogin) -> NoReturn:
        actor = request.user if request.user.is_authenticated else None
        record_social_refusal(
            request, sociallogin.account.provider, "reauthentication_required", actor
        )
        raise PermissionDenied("Account connection requires current reauthentication.")

    def get_connect_redirect_url(self, request: HttpRequest, socialaccount: SocialAccount) -> str:
        return social_callback_url()

    def validate_disconnect(
        self, account: SocialAccount, accounts: QuerySet[SocialAccount]
    ) -> None:
        user = account.user
        password_login = (
            user.has_usable_password()
            and EmailAddress.objects.filter(user=user, verified=True).exists()
        )
        other_enabled_provider = (
            accounts.exclude(pk=account.pk)
            .filter(provider__in=settings.SOCIALACCOUNT_PROVIDERS)
            .exists()
        )
        if not password_login and not other_enabled_provider:
            raise self.validation_error("disconnect_last")
