from allauth.headless.account import views as account_views
from allauth.headless.base.views import ConfigView
from allauth.headless.constants import Client
from allauth.headless.mfa import views as mfa_views
from django.urls import path
from django.views.decorators.http import require_POST

from pyracast.accounts import headless
from pyracast.accounts.mfa_views import (
    AuthenticateView,
    ManageRecoveryCodesView,
    ManageTOTPView,
    ReauthenticateView,
)
from pyracast.accounts.signup import CompanySignupView
from pyracast.accounts.social_views import (
    ManageProvidersView,
    ProviderSignupView,
    RedirectToProviderView,
)

# Only the allauth views the web client calls are routed; a library upgrade adds none.
urlpatterns = [
    path("config", ConfigView.as_api_view(client=Client.BROWSER)),
    path("auth/session", account_views.SessionView.as_api_view(client=Client.BROWSER)),
    path("auth/login", headless.LoginView.as_api_view(client=Client.BROWSER)),
    path("auth/signup", CompanySignupView.as_api_view(client=Client.BROWSER)),
    path(
        "auth/email/verify",
        require_POST(headless.VerifyEmailView.as_api_view(client=Client.BROWSER)),
    ),
    path(
        "auth/email/verify/resend",
        headless.ResendEmailVerificationCodeView.as_api_view(client=Client.BROWSER),
    ),
    path(
        "auth/password/request",
        headless.RequestPasswordResetView.as_api_view(client=Client.BROWSER),
    ),
    path(
        "auth/password/reset",
        require_POST(headless.ResetPasswordView.as_api_view(client=Client.BROWSER)),
    ),
    path("auth/reauthenticate", headless.ReauthenticateView.as_api_view(client=Client.BROWSER)),
    path(
        "auth/reauthenticate/code/request",
        account_views.RequestReauthenticationCodeView.as_api_view(client=Client.BROWSER),
    ),
    path(
        "auth/reauthenticate/code/confirm",
        account_views.ConfirmReauthenticationCodeView.as_api_view(client=Client.BROWSER),
    ),
    path("auth/2fa/authenticate", AuthenticateView.as_api_view(client=Client.BROWSER)),
    path("auth/2fa/reauthenticate", ReauthenticateView.as_api_view(client=Client.BROWSER)),
    path(
        "auth/provider/redirect",
        RedirectToProviderView.as_api_view(client=Client.BROWSER),
        name="provider_redirect",
    ),
    path("auth/provider/signup", ProviderSignupView.as_api_view(client=Client.BROWSER)),
    path("account/email", headless.ManageEmailView.as_api_view(client=Client.BROWSER)),
    path("account/password/change", headless.ChangePasswordView.as_api_view(client=Client.BROWSER)),
    path("account/providers", ManageProvidersView.as_api_view(client=Client.BROWSER)),
    path("account/authenticators", mfa_views.AuthenticatorsView.as_api_view(client=Client.BROWSER)),
    path("account/authenticators/totp", ManageTOTPView.as_api_view(client=Client.BROWSER)),
    path(
        "account/authenticators/recovery-codes",
        ManageRecoveryCodesView.as_api_view(client=Client.BROWSER),
    ),
]
