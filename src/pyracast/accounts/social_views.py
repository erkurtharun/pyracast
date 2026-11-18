from typing import Any, cast
from urllib.parse import urlsplit

from allauth.account.internal.flows import reauthentication
from allauth.core import ratelimit
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.headless.base.response import AuthenticationResponse, ConflictResponse
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
from allauth.socialaccount.internal.flows.signup import get_pending_signup
from allauth.socialaccount.models import SocialAccount, SocialApp
from allauth.socialaccount.providers import registry
from allauth.socialaccount.providers.base.provider import Provider, ProviderAccount
from allauth.socialaccount.providers.google.views import oauth2_callback as google_callback
from allauth.socialaccount.providers.microsoft.views import oauth2_callback as microsoft_callback
from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    HttpResponseRedirect,
)
from django.template.loader import render_to_string
from django.views.decorators.http import require_GET

from pyracast.accounts.processes import PROVIDER_SIGNUP_SESSION_KEY, lock_account, starting
from pyracast.accounts.social import (
    locked_social_user,
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
        refused = HttpResponseRedirect(f"{callback}&error=invalid_request")
        if request.POST.get("callback_url") != callback:
            return refused
        if request.POST.get(
            "process"
        ) == "connect" and reauthentication.is_reauthentication_required(
            request, app="allauth.socialaccount"
        ):
            return HttpResponseRedirect(
                f"{callback}&error=reauthentication_required&error_process=connect"
            )
        form = RedirectToProviderForm(request.POST)
        if not form.is_valid():
            return refused
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

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method != "DELETE" or not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        with locked_social_user(request, request.user.pk) as user:
            if user is None:
                return AuthenticationResponse(request)
            return super().dispatch(request, *args, **kwargs)


@require_GET
def provider_callback(request: HttpRequest, provider: str) -> HttpResponseBase:
    if provider not in settings.SOCIALACCOUNT_PROVIDERS:
        user = request.user if request.user.is_authenticated else None
        record_social_refusal(request, provider, "provider_disabled", user)
        callback = social_callback_url()
        return HttpResponseRedirect(f"{callback}&error=provider_unavailable")
    callback = google_callback if provider == "google" else microsoft_callback
    return starting(request, lambda: cast(HttpResponseBase, callback(request)))
