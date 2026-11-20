from typing import Any, cast

from allauth.account import signals as account_signals
from allauth.account.models import EmailAddress
from allauth.headless.adapter import DefaultHeadlessAdapter
from allauth.mfa.models import Authenticator
from allauth.socialaccount import signals as social_signals
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.internal import statekit
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin
from allauth.socialaccount.providers.base.provider import Provider
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils.crypto import salted_hmac

from pyracast.accounts.audit import SecurityEvent, record_security_event, refusal_throttle
from pyracast.accounts.authorization import requires_admin_mfa
from pyracast.accounts.processes import identified_accounts, lock_account
from pyracast.accounts.session_cookie import SessionStore
from pyracast.db import violated_constraint

# Django's generated name for allauth's unique provider and uid.
IDENTITY_CONSTRAINT = "socialaccount_socialaccount_provider_uid_fc810c6e_uniq"


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


def _verified_login_key(session_key: str) -> str:
    digest = salted_hmac("pyracast.accounts.verified-login", session_key, algorithm="sha256")
    return f"provider-login:{digest.hexdigest()}"


def store_verified_login(session_key: str, login: SocialLogin) -> None:
    # Bound to the browser session that started the round trip, for as long as its state lives.
    cache.set(_verified_login_key(session_key), login.serialize(), statekit.STATE_TTL)


def take_verified_login(request: HttpRequest) -> SocialLogin | None:
    # Once: the delete decides. Only the stored session the request carries finds it, so a login
    # stored before that session ended (a sign-out, a password change) is never found.
    session_key = cast(SessionStore, request.session).stored_key()
    if session_key is None:
        return None
    key = _verified_login_key(session_key)
    data = cache.get(key)
    if data is None or not cache.delete(key):
        return None
    try:
        return SocialLogin.deserialize(data)
    except ValueError:
        return None


def connect_identity(request: HttpRequest, login: SocialLogin, user: User) -> bool:
    # Inside the credential write, after the account's locks. The identity's owner is read without
    # a lock, so a connection never locks or waits for another account's rows; False when another
    # account holds the identity.
    account = login.account
    identity = SocialAccount.objects.filter(provider=account.provider, uid=account.uid)
    owner = identity.values_list("user_id", flat=True).first()
    if owner == user.pk:
        identity.update(extra_data=account.extra_data)
        return True
    if owner is not None:
        return False
    try:
        with transaction.atomic():
            # allauth saves the whole account row; the row as locked here is written back as is.
            login.connect(request, User.objects.get(pk=user.pk))
    except IntegrityError as error:
        if violated_constraint(error) != IDENTITY_CONSTRAINT:
            raise
        return False
    return True


def lock_identity(provider: str, uid: str) -> None:
    # A sign-in's completion locks the account a provider identity names before allauth looks the
    # identity up; the owner is read again under the lock.
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
        # ever stored.
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
        if request.user.is_authenticated:
            record_social_refusal(request, provider, "signed_in", request.user)
            raise PermissionDenied("Sign out before signing in with a provider.")
        if user is not None and user.pk not in identified_accounts():
            # The identity was connected after the completion looked for it, so its account was
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
        record_social_refusal(request, provider.id, "provider_error")
        super().on_authentication_error(
            request, provider, error=error, exception=exception, extra_context=extra_context
        )

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
