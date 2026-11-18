from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from typing import Any, cast

from allauth.account.internal.flows import reauthentication
from allauth.account.models import EmailAddress
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.mfa.models import Authenticator
from allauth.socialaccount import signals
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

from pyracast.accounts.audit import record_security_event
from pyracast.accounts.authorization import requires_admin_mfa


def social_callback_url() -> str:
    return f"{settings.PYRACAST_PUBLIC_ORIGIN}/app/?social_callback=1"


class UnavailableSocialAppError(SocialApp.DoesNotExist, ValueError):
    pass


@contextmanager
def locked_social_user(request: HttpRequest, user_id: int) -> Iterator[User | None]:
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=user_id).first()
        request.user = get_user(request)
        yield (
            request.user
            if user is not None and request.user.is_authenticated and request.user.pk == user.pk
            else None
        )


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

    def pre_social_login(self, request: HttpRequest, sociallogin: SocialLogin) -> None:
        provider = sociallogin.account.provider
        data = sociallogin.account.extra_data
        for address in sociallogin.email_addresses:
            authoritative_google_email = provider == "google" and (
                address.email.lower().endswith("@gmail.com") or bool(data.get("hd"))
            )
            address.verified = address.verified and authoritative_google_email
        sociallogin.account.extra_data = {
            "email": data.get("email") or data.get("mail") or data.get("userPrincipalName") or ""
        }
        sociallogin.token = None
        if sociallogin.is_existing:
            sociallogin.account.save(update_fields=["extra_data"])
        if provider not in settings.SOCIALACCOUNT_PROVIDERS:
            raise PermissionDenied("This sign-in provider is unavailable.")
        if sociallogin.state.get("process") == "connect":
            self.connect_account(request, sociallogin)
        if sociallogin.is_existing:
            user = cast(User, sociallogin.user)
            if (
                requires_admin_mfa(user)
                and not Authenticator.objects.filter(
                    user=user, type=Authenticator.Type.TOTP
                ).exists()
            ):
                raise PermissionDenied(
                    "Administrator sign-in requires a provisioned authenticator."
                )

    def connect_account(self, request: HttpRequest, sociallogin: SocialLogin) -> None:
        origin = sociallogin.state.get("data") or {}
        user_id = origin.get("user_id")
        if not isinstance(user_id, int):
            raise PermissionDenied("Account connection requires an initiating user.")
        with locked_social_user(request, user_id) as user:
            if user is None or reauthentication.is_reauthentication_required(
                request, app="allauth.socialaccount"
            ):
                raise PermissionDenied("Account connection requires current reauthentication.")
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
        raise ImmediateHttpResponse(
            HttpResponseRedirect(self.get_connect_redirect_url(request, sociallogin.account))
        )

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


def _record_social_event(
    event: str,
    *,
    request: HttpRequest,
    sociallogin: SocialLogin | None = None,
    socialaccount: SocialAccount | None = None,
    **_kwargs: Any,
) -> None:
    account = socialaccount if socialaccount is not None else cast(SocialLogin, sociallogin).account
    record_security_event(event, request=request, user=account.user)


for _signal, _event in (
    (signals.social_account_added, "social_account_connected"),
    (signals.social_account_removed, "social_account_disconnected"),
):
    _signal.connect(
        partial(_record_social_event, _event),
        weak=False,
        dispatch_uid=f"pyracast.security.{_event}",
    )
