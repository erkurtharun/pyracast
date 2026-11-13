from allauth.headless.account.views import VerifyEmailView
from allauth.headless.constants import Client
from django.urls import include, path
from django.views.decorators.http import require_POST
from drf_spectacular.views import SpectacularAPIView
from rest_framework.permissions import IsAuthenticated

from pyracast import health
from pyracast.accounts import headless
from pyracast.accounts.mfa_views import ManageRecoveryCodesView
from pyracast.accounts.signup import CompanySignupView
from pyracast.admin import site
from pyracast.api.session import SessionView
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
from pyracast.forecasts.views import CollectionScheduleView
from pyracast.plants.site_api import SitesView, SiteView

urlpatterns = [
    path("health/live/", health.live),
    path("health/ready/", health.ready),
    path("admin/", site.urls),
    path("api/auth/browser/v1/auth/login", headless.LoginView.as_api_view(client=Client.BROWSER)),
    path(
        "api/auth/browser/v1/auth/email/verify",
        require_POST(VerifyEmailView.as_api_view(client=Client.BROWSER)),
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
    path("api/schema/", SpectacularAPIView.as_view(permission_classes=[IsAuthenticated])),
    path("api/session/", SessionView.as_view()),
    path("api/", include("pyracast.notifications.urls")),
    path("api/tenants/", TenantsView.as_view()),
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
