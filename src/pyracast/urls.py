from allauth.headless.account.views import VerifyEmailView
from allauth.headless.constants import Client
from django.urls import include, path
from django.views.decorators.http import require_POST
from drf_spectacular.views import SpectacularAPIView
from rest_framework.permissions import IsAuthenticated

from pyracast import health
from pyracast.accounts import headless
from pyracast.accounts.mfa_views import ManageRecoveryCodesView, ManageTOTPView
from pyracast.accounts.signup import CompanySignupView
from pyracast.accounts.social_views import (
    ManageProvidersView,
    ProviderSignupView,
    RedirectToProviderView,
    disabled_provider_token,
    provider_callback,
)
from pyracast.admin import site
from pyracast.api import error_pages
from pyracast.api.session import SessionLanguageView, SessionView
from pyracast.api.trash import RestoreRegionView, TrashedPlantsView, TrashedRegionsView
from pyracast.api.views import (
    AccountView,
    ApproveView,
    BindingView,
    ForecastView,
    LocationChangeRequestsView,
    RegionsView,
    RegionView,
    RotateSecretView,
    TenantsView,
    TransitionView,
    ValidateView,
)
from pyracast.api.workspaces import (
    InvitationAcceptanceView,
    WorkspaceInvitationsView,
    WorkspaceInvitationView,
    WorkspaceMembersView,
    WorkspaceMemberView,
)
from pyracast.forecasts.views import CollectionScheduleView
from pyracast.plants.site_api import SitesView, SiteView

handler400 = error_pages.bad_request
handler403 = error_pages.permission_denied
handler404 = error_pages.page_not_found
handler500 = error_pages.server_error

urlpatterns = [
    path("health/live/", health.live),
    path("health/ready/", health.ready),
    path("admin/", site.urls),
    path("api/auth/browser/v1/auth/login", headless.LoginView.as_api_view(client=Client.BROWSER)),
    path(
        "api/auth/browser/v1/auth/provider/redirect",
        RedirectToProviderView.as_api_view(client=Client.BROWSER),
    ),
    path(
        "api/auth/browser/v1/auth/provider/signup",
        ProviderSignupView.as_api_view(client=Client.BROWSER),
    ),
    path("api/auth/browser/v1/auth/provider/token", disabled_provider_token),
    path(
        "api/auth/browser/v1/account/providers",
        ManageProvidersView.as_api_view(client=Client.BROWSER),
    ),
    path(
        "api/auth/browser/v1/auth/email/verify",
        require_POST(VerifyEmailView.as_api_view(client=Client.BROWSER)),
    ),
    path(
        "api/auth/browser/v1/account/authenticators/totp",
        ManageTOTPView.as_api_view(client=Client.BROWSER),
    ),
    path(
        "api/auth/browser/v1/account/authenticators/recovery-codes",
        ManageRecoveryCodesView.as_api_view(client=Client.BROWSER),
    ),
    path(
        "api/auth/browser/v1/auth/password/request",
        headless.RequestPasswordResetView.as_api_view(client=Client.BROWSER),
    ),
    path(
        "api/auth/browser/v1/auth/password/reset",
        require_POST(headless.ResetPasswordView.as_api_view(client=Client.BROWSER)),
    ),
    path(
        "api/auth/browser/v1/account/password/change",
        headless.ChangePasswordView.as_api_view(client=Client.BROWSER),
    ),
    path(
        "api/auth/browser/v1/auth/reauthenticate",
        headless.ReauthenticateView.as_api_view(client=Client.BROWSER),
    ),
    path(
        "api/auth/browser/v1/auth/signup",
        CompanySignupView.as_api_view(client=Client.BROWSER),
    ),
    path("api/auth/", include("allauth.headless.urls")),
    path(
        "api/accounts/google/login/callback/",
        provider_callback,
        {"provider": "google"},
        name="google_callback",
    ),
    path(
        "api/accounts/microsoft/login/callback/",
        provider_callback,
        {"provider": "microsoft"},
        name="microsoft_callback",
    ),
    path("api/schema/", SpectacularAPIView.as_view(permission_classes=[IsAuthenticated])),
    path("api/session/", SessionView.as_view()),
    path("api/session/language/", SessionLanguageView.as_view()),
    path("api/", include("pyracast.notifications.urls")),
    path("api/tenants/", TenantsView.as_view()),
    path("api/tenants/<int:tenant_id>/change-requests/", include("pyracast.change_requests.urls")),
    path("api/tenants/<int:tenant_id>/members/", WorkspaceMembersView.as_view()),
    path("api/tenants/<int:tenant_id>/members/<int:user_id>/", WorkspaceMemberView.as_view()),
    path("api/tenants/<int:tenant_id>/invitations/", WorkspaceInvitationsView.as_view()),
    path(
        "api/tenants/<int:tenant_id>/invitations/<int:invitation_id>/",
        WorkspaceInvitationView.as_view(),
    ),
    path("api/invitations/accept/", InvitationAcceptanceView.as_view()),
    path("api/tenants/<int:tenant_id>/sites/", SitesView.as_view()),
    path("api/tenants/<int:tenant_id>/sites/<int:region_id>/", SiteView.as_view()),
    path("api/tenants/<int:tenant_id>/trash/regions/", TrashedRegionsView.as_view()),
    path("api/tenants/<int:tenant_id>/trash/plants/", TrashedPlantsView.as_view()),
    path("api/tenants/<int:tenant_id>/regions/", RegionsView.as_view()),
    path("api/tenants/<int:tenant_id>/regions/<int:region_id>/", RegionView.as_view()),
    path(
        "api/tenants/<int:tenant_id>/regions/<int:region_id>/location-change-requests/",
        LocationChangeRequestsView.as_view(),
    ),
    path(
        "api/tenants/<int:tenant_id>/regions/<int:region_id>/restore/",
        RestoreRegionView.as_view(),
    ),
    path("api/tenants/<int:tenant_id>/regions/<int:region_id>/forecast/", ForecastView.as_view()),
    path(
        "api/tenants/<int:tenant_id>/regions/<int:region_id>/collection-schedule/",
        CollectionScheduleView.as_view(),
    ),
    path(
        "api/tenants/<int:tenant_id>/regions/<int:region_id>/plants/",
        include("pyracast.plants.urls"),
    ),
    path("api/platform/accounts/", AccountView.as_view()),
    path("api/platform/accounts/<int:account_id>/rotate-secret/", RotateSecretView.as_view()),
    path("api/platform/regions/<int:region_id>/bindings/", BindingView.as_view()),
    path("api/platform/bindings/<int:binding_id>/validate/", ValidateView.as_view()),
    path("api/platform/regions/<int:region_id>/approve/", ApproveView.as_view()),
    path("api/platform/regions/<int:region_id>/transition/", TransitionView.as_view()),
]
