from collections.abc import Callable
from typing import Any, cast
from urllib.parse import urlsplit

from allauth.account.internal.flows import reauthentication
from allauth.core import ratelimit
from allauth.core.exceptions import ImmediateHttpResponse, ReauthenticationRequired
from allauth.core.internal.httpkit import add_query_params
from allauth.headless.base.response import ConflictResponse
from allauth.headless.socialaccount.forms import RedirectToProviderForm
from allauth.headless.socialaccount.inputs import SignupInput
from allauth.headless.socialaccount.response import SocialAccountsResponse
from allauth.headless.socialaccount.views import (
    ManageProvidersView as AllauthManageProvidersView,
)
from allauth.headless.socialaccount.views import (
    ProviderSignupView as AllauthProviderSignupView,
)
from allauth.headless.socialaccount.views import (
    RedirectToProviderView as AllauthRedirectToProviderView,
)
from allauth.socialaccount.adapter import get_adapter as get_social_adapter
from allauth.socialaccount.helpers import complete_social_login, render_authentication_error
from allauth.socialaccount.internal.flows.signup import get_pending_signup
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin
from allauth.socialaccount.providers import registry
from allauth.socialaccount.providers.base.constants import AuthError, AuthProcess
from allauth.socialaccount.providers.base.provider import (
    Provider,
    ProviderAccount,
    ProviderException,
)
from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from allauth.socialaccount.providers.microsoft.views import MicrosoftGraphOAuth2Adapter
from allauth.socialaccount.providers.oauth2.client import OAuth2Error
from allauth.socialaccount.providers.oauth2.views import OAuth2Adapter, OAuth2CallbackView
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.exceptions import SessionInterrupted
from django.core.exceptions import ObjectNotUpdated, PermissionDenied
from django.db import DatabaseError, IntegrityError, transaction
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    HttpResponseRedirect,
)
from django.template.loader import render_to_string
from django.views.decorators.http import require_GET
from requests import RequestException

from pyracast.accounts.processes import (
    PROVIDER_SIGNUP_SESSION_KEY,
    credential_write,
    lock_account,
    starting,
)
from pyracast.accounts.social import (
    authorize_connection,
    lock_identity,
    record_social_refusal,
    social_callback_url,
)
from pyracast.accounts.step_up import StepUpAnswers
from pyracast.db import violated_constraint

# A provider sign-up whose identity (Django's generated name for allauth's unique provider and
# uid) or vouched address another account claimed meanwhile answers like nothing pending.
CLAIMED_CONSTRAINTS = {
    "socialaccount_socialaccount_provider_uid_fc810c6e_uniq",
    "unique_verified_email",
}


class RetiredProvider(Provider):
    # A linked identity of a provider this release does not install: listed, never signed in with.
    uses_apps = False
    account_class = ProviderAccount

    def __init__(self, request: HttpRequest, provider_id: str) -> None:
        super().__init__(request)
        self.id = self.name = provider_id


class CompanySocialSignupInput(SignupInput):
    def save(self, request: HttpRequest) -> User:
        # The account keeps only the address typed in the form; it is verified at signup only
        # when the provider vouches for that same address, otherwise by a mailed code.
        typed = str(self.cleaned_data["email"]).lower()
        self.sociallogin.email_addresses = [
            address
            for address in self.sociallogin.email_addresses
            if address.email.lower() == typed
        ]
        with transaction.atomic():
            user = cast(User, super().save(request))
            lock_account(user.pk)
            return user


class ProviderSignupView(AllauthProviderSignupView):
    input_class = CompanySocialSignupInput

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method != "POST":
            return super().handle(request, *args, **kwargs)
        if get_pending_signup(request) is not None:
            limited = ratelimit.consume_or_429(request, action="signup")
            if limited:
                return limited
        pending = request.session.get(PROVIDER_SIGNUP_SESSION_KEY)
        try:
            return starting(
                request, lambda: super(ProviderSignupView, self).handle(request, *args, **kwargs)
            )
        except ImmediateHttpResponse:
            request.session[PROVIDER_SIGNUP_SESSION_KEY] = pending
            raise
        except IntegrityError as error:
            if violated_constraint(error) not in CLAIMED_CONSTRAINTS:
                raise
            return ConflictResponse(request)


class RedirectToProviderView(AllauthRedirectToProviderView):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        callback = social_callback_url()
        if request.POST.get("callback_url") != callback:
            return HttpResponseRedirect(f"{callback}&error=invalid_request")
        if request.POST.get("process") != "connect":
            return self.to_provider(request, callback)
        refused = HttpResponseRedirect(
            f"{callback}&error=reauthentication_required&error_process=connect"
        )
        if not request.user.is_authenticated:
            return refused
        # The pending connection is saved in the same transaction as the check before it.
        try:
            with credential_write(request):
                if reauthentication.is_reauthentication_required(
                    request, app="allauth.socialaccount"
                ):
                    return refused
                return self.to_provider(request, callback)
        except ReauthenticationRequired:
            return refused

    def to_provider(self, request: HttpRequest, callback: str) -> HttpResponse:
        form = RedirectToProviderForm(request.POST)
        if not form.is_valid():
            return HttpResponseRedirect(f"{callback}&error=invalid_request")
        process = form.cleaned_data["process"]
        response = cast(
            HttpResponse,
            form.cleaned_data["provider"].redirect(
                request,
                process,
                next_url=callback,
                data={"user_id": request.user.pk} if process == "connect" else {},
                headless=True,
            ),
        )
        if not isinstance(response, HttpResponseRedirect) or urlsplit(response.url).netloc in {
            "",
            request.get_host(),
            urlsplit(settings.PYRACAST_PUBLIC_ORIGIN).netloc,
        }:
            return response
        continuation = HttpResponse(
            render_to_string(
                "accounts/provider_redirect.html", {"provider_url": response.url}, request
            ),
            headers={
                key: value
                for key, value in response.items()
                if key.lower() not in {"location", "content-type"}
            },
        )
        continuation.cookies = response.cookies
        continuation["Cache-Control"] = "no-store"
        continuation["Referrer-Policy"] = "no-referrer"
        return continuation


class ManageProvidersView(StepUpAnswers, AllauthManageProvidersView):
    @classmethod
    def respond_provider_accounts(cls, request: HttpRequest) -> SocialAccountsResponse:
        accounts = list(SocialAccount.objects.filter(user=cast(User, request.user)))
        for account in accounts:
            if account.provider not in settings.SOCIALACCOUNT_PROVIDERS:
                cls.prepare_disabled_provider(request, account)
        return SocialAccountsResponse(request, accounts)

    @staticmethod
    def prepare_disabled_provider(request: HttpRequest, account: SocialAccount) -> None:
        registry.load()
        provider_class = registry.provider_map.get(account.provider)
        if provider_class is None:
            account._provider = RetiredProvider(request, account.provider)
            return
        provider = cast(Provider, provider_class(request, app=SocialApp(provider=account.provider)))
        provider.supports_redirect = False
        provider.supports_token_authentication = False
        account._provider = provider

    def delete(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        account = self.input.cleaned_data["account"]
        if account.provider not in settings.SOCIALACCOUNT_PROVIDERS:
            self.prepare_disabled_provider(request, account)
        return super().delete(request, *args, **kwargs)

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method != "DELETE" or not request.user.is_authenticated:
            return super().handle(request, *args, **kwargs)
        with credential_write(request):
            return super().handle(request, *args, **kwargs)


def complete(request: HttpRequest, login: SocialLogin) -> HttpResponseBase:
    # Runs in `starting`, after the provider exchange: the accounts it writes are locked before
    # allauth's lookup reads or refreshes the identity, and before any session row is touched.
    if login.state.get("process") != AuthProcess.CONNECT:
        lock_identity(login.account.provider, login.account.uid)
        return cast(HttpResponseBase, complete_social_login(request, login))
    try:
        authorize_connection(request, login)
    except PermissionDenied:
        error = {"error": "permission_denied", "error_process": AuthProcess.CONNECT}
        return HttpResponseRedirect(add_query_params(login.state["next"], error))
    # allauth's lookup refreshes an existing identity before the connection is decided; a
    # connection refused after it rolls that back too. Only a connection ends the flow by raising.
    with transaction.atomic():
        try:
            refused = cast(HttpResponseBase, complete_social_login(request, login))
        except ImmediateHttpResponse as connected:
            return cast(HttpResponseBase, connected.response)
        transaction.set_rollback(True)
    return refused


class ProviderCallbackView(OAuth2CallbackView):
    # allauth contract: this is allauth 65.22.1's OAuth2CallbackView.dispatch step by step
    # (tests/test_provider_callback_contract.py pins that source). Only the transactions differ:
    # the state is taken and saved on its own, no transaction is open during the provider
    # exchange, and the completion runs in `starting`.
    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        adapter: Any = self.adapter
        provider = adapter.get_provider()
        state, response = self._get_state(request, provider)
        if response:
            return cast(HttpResponseBase, response)
        if "error" in request.GET or "code" not in request.GET:
            cancelled = request.GET.get("error", None) == adapter.login_cancelled_error
            return cast(
                HttpResponseBase,
                render_authentication_error(
                    request,
                    provider,
                    error=AuthError.CANCELLED if cancelled else AuthError.UNKNOWN,
                    extra_context={"state": state, "callback_view": self},
                ),
            )
        app = provider.app
        client = adapter.get_client(self.request, app)
        try:
            access_token = adapter.get_access_token_data(
                request, app, client, pkce_code_verifier=state.get("pkce_code_verifier")
            )
            token = adapter.parse_token(access_token)
            login = adapter.complete_login(request, app, token, response=access_token)
            login.token = token
            login.state = state
            return starting(request, lambda: complete(request, login))
        except (PermissionDenied, OAuth2Error, RequestException, ProviderException) as error:
            return cast(
                HttpResponseBase,
                render_authentication_error(
                    request, provider, exception=error, extra_context={"state": state}
                ),
            )


callback_view: Callable[[type[OAuth2Adapter]], Callable[[HttpRequest], HttpResponseBase]] = (
    ProviderCallbackView.adapter_view
)
CALLBACKS = {
    "google": callback_view(GoogleOAuth2Adapter),
    "microsoft": callback_view(MicrosoftGraphOAuth2Adapter),
}


@require_GET
def provider_callback(request: HttpRequest, provider: str) -> HttpResponseBase:
    if provider not in settings.SOCIALACCOUNT_PROVIDERS:
        user = request.user if request.user.is_authenticated else None
        record_social_refusal(request, provider, "provider_disabled", user)
        callback = social_callback_url()
        return HttpResponseRedirect(f"{callback}&error=provider_unavailable")
    try:
        return CALLBACKS[provider](request)
    except UpdateError as error:
        # allauth saves the session as it takes the provider state, and a session that ended
        # before fails that save. Django raises the same error for any failed save, such as a
        # lock timeout, which leaves the session and its state; that failure is answered as
        # itself.
        failure = error.__context__
        if isinstance(failure, DatabaseError) and not isinstance(failure, ObjectNotUpdated):
            raise failure from failure.__cause__
        return nothing_pending(request, provider)
    except SessionInterrupted:
        # The session ended during the provider exchange; its completion found it gone.
        return nothing_pending(request, provider)


def nothing_pending(request: HttpRequest, provider: str) -> HttpResponseBase:
    # Nothing is pending for an ended session: answer as then, on an empty session that sends no
    # cookie.
    request.session = type(request.session)()
    social_provider = get_social_adapter(request).get_provider(request, provider)
    return cast(HttpResponseBase, render_authentication_error(request, social_provider))
