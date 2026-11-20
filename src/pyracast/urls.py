from django.urls import include, path

from pyracast import health
from pyracast.accounts.social_views import provider_callback
from pyracast.api import error_pages
from pyracast.api.csp_reports import CSPReportView
from pyracast.api.session import SessionLanguageView, SessionView
from pyracast.api.trash import RestoreRegionView, TrashedRegionsView
from pyracast.api.views import (
    ForecastView,
    LocationChangeRequestsView,
    LocationChangeWithdrawView,
    RegionView,
    TenantsView,
)
from pyracast.api.workspaces import (
    InvitationAcceptanceView,
    WorkspaceInvitationsView,
    WorkspaceInvitationView,
    WorkspaceMembersView,
    WorkspaceMemberView,
)
from pyracast.forecasts.views import CollectionScheduleView
from pyracast.plants.site_api import SitesView
from pyracast.platform_admin.site import site

handler400 = error_pages.bad_request
handler403 = error_pages.permission_denied
handler404 = error_pages.page_not_found
handler500 = error_pages.server_error

urlpatterns = [
    path("health/live/", health.live),
    path("health/ready/", health.ready),
    path("admin/", site.urls),
    path("api/auth/browser/v1/", include("pyracast.accounts.auth_urls")),
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
    path("api/csp-reports/", CSPReportView.as_view()),
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
    path("api/tenants/<int:tenant_id>/trash/regions/", TrashedRegionsView.as_view()),
    path("api/tenants/<int:tenant_id>/regions/<int:region_id>/", RegionView.as_view()),
    path(
        "api/tenants/<int:tenant_id>/regions/<int:region_id>/location-change-requests/",
        LocationChangeRequestsView.as_view(),
    ),
    path(
        "api/tenants/<int:tenant_id>/regions/<int:region_id>/location-change-requests/"
        "<int:request_id>/withdraw/",
        LocationChangeWithdrawView.as_view(),
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
]
