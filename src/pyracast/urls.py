from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView
from rest_framework.permissions import IsAuthenticated

from pyracast.admin import site
from pyracast.api.session import SessionLoginView, SessionLogoutView, SessionView
from pyracast.api.views import (
    AccountView,
    ApproveView,
    BindingView,
    ForecastView,
    RegionsView,
    RegionView,
    RotateSecretView,
    TenantsView,
    TransitionView,
    ValidateView,
)
from pyracast.forecasts.views import CollectionScheduleView

urlpatterns = [
    path("admin/", site.urls),
    path("auth/", include("rest_framework.urls")),
    path("api/schema/", SpectacularAPIView.as_view(permission_classes=[IsAuthenticated])),
    path("api/session/", SessionView.as_view()),
    path("api/session/login/", SessionLoginView.as_view()),
    path("api/session/logout/", SessionLogoutView.as_view()),
    path("api/", include("pyracast.notifications.urls")),
    path("api/tenants/", TenantsView.as_view()),
    path("api/tenants/<int:tenant_id>/regions/", RegionsView.as_view()),
    path("api/tenants/<int:tenant_id>/regions/<int:region_id>/", RegionView.as_view()),
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
