from typing import Any, cast
from urllib.parse import urlsplit

from allauth.account.internal.flows import reauthentication
from allauth.core import ratelimit
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.headless.base.response import AuthenticationResponse
from allauth.headless.internal.restkit.response import ErrorResponse
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
from allauth.socialaccount.models import SocialAccount, SocialApp
from allauth.socialaccount.providers.google.provider import GoogleProvider
from allauth.socialaccount.providers.google.views import oauth2_callback as google_callback
from allauth.socialaccount.providers.microsoft.provider import MicrosoftGraphProvider
from allauth.socialaccount.providers.microsoft.views import oauth2_callback as microsoft_callback
from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    HttpResponseNotFound,
    HttpResponseRedirect,
)
from django.template.loader import render_to_string
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods

from pyracast.accounts.social import locked_social_user


class CompanySocialSignupInput(SignupInput):
    @transaction.atomic
    def save(self, request: HttpRequest) -> User:
        try:
            with transaction.atomic():
                return cast(User, super().save(request))
        except IntegrityError as error:
            if (
                getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
                != "unique_workspace_name"
            ):
                raise
            self.add_error("company_name", _("This workspace name is already in use."))
            raise ImmediateHttpResponse(ErrorResponse(request, input=self)) from error


class ProviderSignupView(AllauthProviderSignupView):
    input_class = CompanySocialSignupInput

    def handle(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method == "POST":
            limited = ratelimit.consume_or_429(request, action="signup")
            if limited:
                return limited
        return super().handle(request, *args, **kwargs)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        pending = request.session.get("socialaccount_sociallogin")
        try:
            return super().post(request, *args, **kwargs)
        except ImmediateHttpResponse:
            request.session["socialaccount_sociallogin"] = pending
            raise


class RedirectToProviderView(AllauthRedirectToProviderView):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        callback = f"{settings.PYRACAST_PUBLIC_ORIGIN}/?social_callback=1"
        if request.POST.get("callback_url") != callback:
            return ErrorResponse(request, status=400)
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
            return super().post(request, *args, **kwargs)
        response = cast(
            HttpResponse,
            form.cleaned_data["provider"].redirect(
                request,
                form.cleaned_data["process"],
                next_url=callback,
                data={"user_id": request.user.pk} if request.user.is_authenticated else {},
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


class ManageProvidersView(AllauthManageProvidersView):
    @classmethod
    def respond_provider_accounts(cls, request: HttpRequest) -> SocialAccountsResponse:
        accounts = list(SocialAccount.objects.filter(user=cast(User, request.user)))
        for account in accounts:
            if account.provider not in settings.SOCIALACCOUNT_PROVIDERS:
                cls.prepare_disabled_provider(request, account)
        return SocialAccountsResponse(request, accounts)

    @staticmethod
    def prepare_disabled_provider(request: HttpRequest, account: SocialAccount) -> None:
        provider_class = {"google": GoogleProvider, "microsoft": MicrosoftGraphProvider}[
            account.provider
        ]
        provider = provider_class(request, app=SocialApp(provider=account.provider))
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


@require_http_methods(["GET", "POST"])
def disabled_provider_token(request: HttpRequest) -> HttpResponseNotFound:
    return HttpResponseNotFound()


@require_GET
def provider_callback(request: HttpRequest, provider: str) -> HttpResponseBase:
    if provider not in settings.SOCIALACCOUNT_PROVIDERS:
        return HttpResponseRedirect(
            f"{settings.PYRACAST_PUBLIC_ORIGIN}/?social_callback=1&error=provider_unavailable"
        )
    callback = google_callback if provider == "google" else microsoft_callback
    return cast(HttpResponseBase, callback(request))
