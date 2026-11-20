from collections.abc import Callable
from functools import partial, update_wrapper
from typing import Any, cast

from django.contrib import admin
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import URLPattern, URLResolver, path
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe
from django.views.generic import RedirectView

from pyracast.accounts.audit import SecurityEvent, record_refusal
from pyracast.accounts.authorization import has_platform_admin_session
from pyracast.accounts.security import has_mfa_evidence
from pyracast.errors import ErrorCode
from pyracast.platform_admin import access, pages, plant_review, tenant_views
from pyracast.platform_admin.failures import contained
from pyracast.platform_admin.forms import ConfirmationForm
from pyracast.platform_admin.historical import historical_import
from pyracast.platform_admin.navigation import (
    SAFE_METHODS,
    console_destination,
    console_path,
    sign_in,
)
from pyracast.platform_admin.plants import new_plant, plant_details_page
from pyracast.platform_admin.read_models import overview_context
from pyracast.platform_admin.records import register_records

_REPLACED_SITE_ROUTES = {"autocomplete", "logout", "password_change", "password_change_done"}
PLANT = "plants/<int:region_id>/"
WORKSPACE = "workspaces/<int:tenant_id>/"


class PlatformAdminSite(admin.AdminSite):
    def has_permission(self, request: HttpRequest) -> bool:
        return has_platform_admin_session(request)

    @method_decorator(never_cache)
    def login(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        target = console_path(request, request.GET.get(REDIRECT_FIELD_NAME))
        if self.has_permission(request):
            return redirect(target or "admin:index")
        if request.user.is_authenticated:
            record_refusal(
                SecurityEvent.AUTHORIZATION_DENIED,
                request=request,
                user_id=request.user.pk,
                view="admin/login/",
                reason=ErrorCode.PLATFORM_ADMIN_REQUIRED.value,
                tenant_id=None,
            )
            return redirect("/app/")
        return redirect(sign_in(target or "/admin/"))

    def admin_view[V: Callable[..., Any]](
        self, view: V, cacheable: bool = False, *, needs_confirmation: bool = True
    ) -> V:
        # Django and every ModelAdmin build their console routes through here too, so an unsafe
        # request reaches a view only with a recent confirmation by the authenticator.
        view = contained(view)
        if needs_confirmation:
            view = self._confirmed(view)
        return super().admin_view(view, cacheable)

    def _confirmed[V: Callable[..., Any]](self, view: V) -> V:
        def confirmed(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
            if request.method in SAFE_METHODS or has_mfa_evidence(request, recent=True):
                return view(request, *args, **kwargs)
            return self.confirmation(
                request,
                ConfirmationForm(user=request.user),
                console_destination(request, request.headers.get("Referer")),
                status=403,
            )

        return cast(V, update_wrapper(confirmed, view))

    def each_context(self, request: HttpRequest) -> dict[str, Any]:
        return {
            **super().each_context(request),
            "confirmation_needed": not has_mfa_evidence(request, recent=True),
        }

    def get_urls(self) -> list[URLPattern | URLResolver]:
        console = (
            ("plants/", partial(pages.plants, site=self), "workspace_plants"),
            ("operations/", partial(pages.operations, site=self), "workspace_operations"),
            ("settings/", partial(pages.settings, site=self), "workspace_settings"),
            ("language/", access.language, "workspace_language"),
            (PLANT, partial(plant_review.plant, site=self), "workspace_plant"),
            (f"{PLANT}decision/", plant_review.decision, "workspace_decision"),
            (f"{PLANT}details/", partial(plant_details_page, site=self), "workspace_plant_details"),
            (
                f"{PLANT}historical-import/",
                partial(historical_import, site=self),
                "workspace_historical_import",
            ),
            ("workspaces/", partial(tenant_views.workspaces, site=self), "workspace_tenants"),
            (
                "workspaces/new/",
                partial(tenant_views.new_workspace, site=self),
                "workspace_tenant_new",
            ),
            (WORKSPACE, partial(tenant_views.workspace, site=self), "workspace_tenant"),
            (
                f"{WORKSPACE}name/",
                partial(tenant_views.workspace_name, site=self),
                "workspace_tenant_name",
            ),
            (
                f"{WORKSPACE}administrator/",
                partial(tenant_views.workspace_administrator, site=self),
                "workspace_tenant_administrator",
            ),
            (
                f"{WORKSPACE}invitations/new/",
                partial(tenant_views.new_invitation, site=self),
                "workspace_invitation_new",
            ),
            (
                f"{WORKSPACE}invitations/<int:invitation_id>/reissue/",
                tenant_views.invitation_reissue,
                "workspace_invitation_reissue",
            ),
            (
                f"{WORKSPACE}invitations/<int:invitation_id>/revocation/",
                tenant_views.invitation_revocation,
                "workspace_invitation_revocation",
            ),
            (
                f"{WORKSPACE}members/<int:user_id>/removal/",
                partial(tenant_views.member_removal, site=self),
                "workspace_member_removal",
            ),
            (f"{WORKSPACE}plants/new/", partial(new_plant, site=self), "workspace_plant_new"),
            ("password_change/", RedirectView.as_view(url="/app/account/"), "password_change"),
        )
        return [
            path("logout/", access.logout, name="logout"),
            path(
                "confirm/",
                self.admin_view(partial(access.confirm, site=self), needs_confirmation=False),
                name="workspace_confirm",
            ),
            *[path(route, self.admin_view(view), name=name) for route, view, name in console],
            *[
                url
                for url in super().get_urls()
                if not isinstance(url, URLPattern) or url.name not in _REPLACED_SITE_ROUTES
            ],
        ]

    def workspace_response(
        self,
        request: HttpRequest,
        section: str,
        title: str,
        context: dict[str, Any],
        *,
        status: int = 200,
        template: str | None = None,
    ) -> TemplateResponse:
        return TemplateResponse(
            request,
            f"admin/workspace/{template or section}.html",
            {
                **self.each_context(request),
                "workspace_section": section,
                "title": title,
                **context,
            },
            status=status,
        )

    @method_decorator(require_safe)
    def index(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> TemplateResponse:
        return self.workspace_response(
            request, "overview", _("Overview"), {**overview_context(), **(extra_context or {})}
        )

    def confirmation(
        self, request: HttpRequest, form: ConfirmationForm, destination: str, *, status: int
    ) -> TemplateResponse:
        return self.workspace_response(
            request,
            "confirm",
            _("Confirm with your authenticator"),
            {"form": form, "next": destination, "refused": status == 403},
            status=status,
        )


site = PlatformAdminSite(name="admin")
site.site_header = gettext_lazy("Pyracast operations")
register_records(site)
