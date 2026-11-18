from functools import partial
from typing import TYPE_CHECKING, Any, cast

from django.contrib import admin, messages
from django.contrib.auth import logout
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import URLPattern, URLResolver, path, reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import SafeString
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.generic import RedirectView
from rest_framework.exceptions import ValidationError as APIValidationError

from pyracast import admin_tenant_views as workspace_views
from pyracast.accounts.authorization import has_platform_admin_session
from pyracast.accounts.language import save_user_language, set_language_cookie
from pyracast.accounts.models import Tenant
from pyracast.admin_forms import (
    BindingActionForm,
    EpiasConnectionForm,
    ProviderSetupForm,
    validation_failure_message,
)
from pyracast.admin_historical import historical_import
from pyracast.admin_sites import new_plant, plant_details_page
from pyracast.admin_workspace import (
    ReviewDecisionForm,
    operations_context,
    overview_context,
    plant_list_context,
    review_context,
    settings_context,
)
from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import SiteBinding
from pyracast.connections.operations import (
    QuotaExceededError,
    RequestDeferredError,
    validate_binding,
)
from pyracast.connections.recovery import clear_provider_cooldown
from pyracast.epias import EpiasError
from pyracast.errors import DomainError, ErrorCode
from pyracast.forecasts.models import (
    CollectionJob,
    DispatcherStatus,
    ForecastResult,
    RequestAttempt,
)
from pyracast.languages import explicit_language
from pyracast.metering.models import EpiasConnection
from pyracast.metering.operations import connect_epias, disconnect_epias
from pyracast.notifications.models import NotificationDelivery, NotificationSettings
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
    PlantSubmission,
    ProductionObservation,
)
from pyracast.plants.operations import configuration_is_current
from pyracast.providers import adapter_for
from pyracast.regions.models import AuditEvent, Region, RegionLocationChangeRequest
from pyracast.regions.operations import approve_region

_REPLACED_SITE_ROUTES = {"autocomplete", "logout", "password_change", "password_change_done"}


class PlatformAdminSite(admin.AdminSite):
    def has_permission(self, request: HttpRequest) -> bool:
        return has_platform_admin_session(request)

    def login(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        if self.has_permission(request):
            return redirect("admin:index")
        return redirect("/app/?next=/admin/")

    def get_urls(self) -> list[URLPattern | URLResolver]:
        return [
            path("plants/", self.admin_view(self.plants), name="workspace_plants"),
            path("logout/", self.admin_view(self.workspace_logout), name="logout"),
            path("language/", self.admin_view(self.language), name="workspace_language"),
            path("plants/<int:object_id>/", self.admin_view(self.plant), name="workspace_plant"),
            path(
                "plants/<int:object_id>/historical-import/",
                self.admin_view(self.historical_import),
                name="workspace_historical_import",
            ),
            path(
                "plants/<int:object_id>/decision/",
                self.admin_view(self.decision),
                name="workspace_decision",
            ),
            path(
                "plants/<int:object_id>/details/",
                self.admin_view(partial(plant_details_page, site=self)),
                name="workspace_plant_details",
            ),
            path("operations/", self.admin_view(self.operations), name="workspace_operations"),
            path("settings/", self.admin_view(self.settings), name="workspace_settings"),
            *self.workspace_urls(),
            path(
                "password_change/",
                self.admin_view(RedirectView.as_view(url="/app/?account=security")),
                name="password_change",
            ),
            *[
                url
                for url in super().get_urls()
                if not isinstance(url, URLPattern) or url.name not in _REPLACED_SITE_ROUTES
            ],
        ]

    def workspace_urls(self) -> list[URLPattern]:
        workspace = "workspaces/<int:tenant_id>/"
        routes = (
            ("workspaces/", partial(workspace_views.workspaces, site=self), "workspace_tenants"),
            (
                "workspaces/new/",
                partial(workspace_views.new_workspace, site=self),
                "workspace_tenant_new",
            ),
            (workspace, partial(workspace_views.workspace, site=self), "workspace_tenant"),
            (
                f"{workspace}name/",
                partial(workspace_views.workspace_name, site=self),
                "workspace_tenant_name",
            ),
            (
                f"{workspace}administrator/",
                partial(workspace_views.workspace_administrator, site=self),
                "workspace_tenant_administrator",
            ),
            (
                f"{workspace}invitations/new/",
                partial(workspace_views.new_invitation, site=self),
                "workspace_invitation_new",
            ),
            (
                f"{workspace}invitations/<int:invitation_id>/reissue/",
                workspace_views.invitation_reissue,
                "workspace_invitation_reissue",
            ),
            (
                f"{workspace}invitations/<int:invitation_id>/revocation/",
                workspace_views.invitation_revocation,
                "workspace_invitation_revocation",
            ),
            (
                f"{workspace}members/<int:user_id>/removal/",
                partial(workspace_views.member_removal, site=self),
                "workspace_member_removal",
            ),
            (f"{workspace}plants/new/", partial(new_plant, site=self), "workspace_plant_new"),
        )
        return [path(route, self.admin_view(view), name=name) for route, view, name in routes]

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

    def index(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> TemplateResponse:
        return self.workspace_response(
            request, "overview", _("Overview"), {**overview_context(), **(extra_context or {})}
        )

    def plants(self, request: HttpRequest) -> HttpResponse:
        context = plant_list_context(
            request.GET.get("q", "").strip()[:200],
            request.GET.get("status", ""),
            request.GET.get("page", "1"),
            request.GET.get("issue", "")[:80],
        )
        query = request.GET.copy()
        query.pop("page", None)
        context["pagination_query"] = query.urlencode()
        return self.workspace_response(request, "plants", _("Plants"), context)

    def plant(self, request: HttpRequest, object_id: int) -> HttpResponse:
        return cast(RegionAdmin, self._registry[Region]).setup(request, object_id)

    def historical_import(self, request: HttpRequest, object_id: int) -> HttpResponse:
        return historical_import(self, request, object_id)

    def operations(self, request: HttpRequest) -> HttpResponse:
        return self.workspace_response(
            request,
            "operations",
            _("Operations"),
            operations_context(request.GET.get("issue", "")[:80]),
        )

    def settings(self, request: HttpRequest) -> HttpResponse:
        if request.method not in {"GET", "POST"}:
            return HttpResponseNotAllowed(("GET", "POST"))
        context = settings_context(request.POST if request.method == "POST" else None)
        form = context["settings_form"]
        if request.method == "POST" and form.is_valid():
            try:
                form.apply(cast(User, request.user))
            except ValidationError:
                form.add_error(None, _("The settings changed. Reload this page before saving."))
            else:
                messages.success(request, _("Email reminder settings saved."))
                return redirect("admin:workspace_settings")
        return self.workspace_response(
            request,
            "settings",
            _("Settings"),
            context,
            status=400 if request.method == "POST" else 200,
        )

    def decision(self, request: HttpRequest, object_id: int) -> HttpResponse:
        if request.method != "POST":
            return HttpResponseNotAllowed(("POST",))
        get_object_or_404(Region, pk=object_id, deleted_at__isnull=True)
        form = ReviewDecisionForm(request.POST)
        if form.is_valid():
            try:
                form.apply(cast(User, request.user), object_id)
            except ObjectDoesNotExist, ValidationError:
                messages.error(
                    request,
                    _("The review could not be applied. Reload and review the current plant."),
                )
            else:
                messages.success(request, _("Plant review saved."))
        else:
            messages.error(
                request, _("Choose a review action and reload the current plant before continuing.")
            )
        return redirect("admin:workspace_plant", object_id=object_id)

    def workspace_logout(self, request: HttpRequest) -> HttpResponse:
        if request.method != "POST":
            return HttpResponseNotAllowed(("POST",))
        logout(request)
        return redirect("/app/")

    def language(self, request: HttpRequest) -> HttpResponse:
        if request.method != "POST":
            return HttpResponseNotAllowed(("POST",))
        language = explicit_language(request.POST.get("language"))
        if language is None:
            return HttpResponse(status=400)
        save_user_language(cast(User, request.user), language)
        request.LANGUAGE_CODE = language
        destination = request.POST.get("next", "/admin/")
        if not url_has_allowed_host_and_scheme(
            destination, {request.get_host()}, require_https=request.is_secure()
        ):
            destination = "/admin/"
        response = redirect(destination)
        set_language_cookie(response, language)
        return response


site = PlatformAdminSite(name="admin")
site.site_header = gettext_lazy("Pyracast operations")


if TYPE_CHECKING:
    AdminBase = admin.ModelAdmin[Any]
else:
    AdminBase = admin.ModelAdmin


class ReadOnlyAdmin(AdminBase):
    def get_urls(self) -> list[URLPattern]:
        prefix = f"{self.opts.app_label}_{self.opts.model_name}_"
        kept = {prefix + view for view in ("changelist", "history", "change")}
        return [url for url in super().get_urls() if url.name in kept]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(NotificationSettings, site=site)
class NotificationSettingsAdmin(ReadOnlyAdmin):
    fields = ("max_recipients",)
    list_display = ("max_recipients",)


@admin.register(DispatcherStatus, site=site)
class DispatcherStatusAdmin(ReadOnlyAdmin):
    list_display = ("phase", "attempted_at", "succeeded_at", "last_error")


@admin.register(NotificationDelivery, site=site)
class NotificationDeliveryAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "recipients",
        "scheduled_for",
        "status",
        "attempts",
        "last_error",
        "sent_at",
    )
    list_filter = ("status",)
    search_fields = ("recipients", "schedule__region__name")
    fields = (
        "schedule",
        "recipients",
        "scheduled_for",
        "status",
        "available_at",
        "attempts",
        "binding",
        "snapshot",
        "preparation_deadline",
        "transmit_started_at",
        "sent_at",
        "last_error",
    )


@admin.register(Region, site=site)
class RegionAdmin(ReadOnlyAdmin):
    list_display = ("id", "name", "tenant", "status", "revision", "review_link")
    list_filter = ("status",)
    search_fields = ("name", "tenant__name")
    change_form_template = "admin/regions/region/change_form.html"

    @admin.display(description=gettext_lazy("Onboarding"))
    def review_link(self, obj: Region) -> SafeString:
        return format_html(
            '<a href="{}">{}</a>',
            reverse("admin:workspace_plant", args=(obj.pk,)),
            _("Review and set up"),
        )

    @method_decorator(sensitive_post_parameters("api_key"))
    @sensitive_variables()
    def setup(self, request: HttpRequest, object_id: int) -> HttpResponse:
        if request.method not in {"GET", "POST"}:
            return HttpResponseNotAllowed(("GET", "POST"))
        region = get_object_or_404(
            Region.objects.select_related("tenant"), pk=object_id, deleted_at__isnull=True
        )
        binding = (
            SiteBinding.objects.select_related("provider_account")
            .filter(region=region, region_revision=region.revision)
            .first()
        )
        plants = Plant.objects.filter(region=region).select_related(
            "current_configuration__binding__region"
        )
        plant = plants.first()
        epias_connection = EpiasConnection.objects.filter(plant=plant).first() if plant else None
        action = request.POST.get("action") if request.method == "POST" else None
        epias_action = action in {"epias_connect", "epias_disconnect"}
        epias_form = EpiasConnectionForm(
            request.POST if action == "epias_connect" else None,
            prefix="epias",
            initial={
                "provider_plant_id": epias_connection.provider_plant_id
                if epias_connection
                else None,
                "expected_eic": epias_connection.eic if epias_connection else "",
            },
        )
        if epias_action and (action == "epias_disconnect" or epias_form.is_valid()):
            try:
                if plant is None:
                    raise DomainError(ErrorCode.CONFIGURATION_UNAVAILABLE)
                actor = cast(User, request.user)
                if action == "epias_disconnect":
                    disconnect_epias(plant.pk, actor=actor)
                    message = _(
                        "EPİAŞ connection disconnected. Saved production remains available."
                    )
                else:
                    connect_epias(plant.pk, actor=actor, **epias_form.cleaned_data)
                    message = _("EPİAŞ connection saved. Initial production collection is queued.")
            except EpiasError as error:
                epias_form.add_error(
                    None,
                    _("EPİAŞ service credentials are unavailable. Contact the deployment operator.")
                    if str(error) == "credentials_unavailable"
                    else _("EPİAŞ is currently unavailable. Try connecting again later."),
                )
            except (ObjectDoesNotExist, ValidationError) as error:
                epias_form.add_error(
                    None,
                    _("The EPİAŞ plant ID and EIC did not match. Check both identifiers.")
                    if isinstance(error, DomainError)
                    and error.code == ErrorCode.EPIAS_PLANT_IDENTITY_MISMATCH
                    else _(
                        "The plant configuration is unavailable or changed. "
                        "Approve its current setup and reload before connecting."
                    ),
                )
            else:
                self.message_user(request, message)
                return redirect("admin:workspace_plant", object_id=region.pk)
        setup_form = ProviderSetupForm(
            request.POST
            if request.method == "POST" and request.POST.get("action") == "configure"
            else None,
            initial={
                "expected_revision": region.revision,
                "expected_binding_id": binding.pk if binding else None,
                "account_mode": "existing" if binding else "new",
                "existing_account": (
                    f"{binding.provider_account_id}:{binding.provider_account.credential_revision}"
                    if binding
                    else ""
                ),
                "site_id": binding.site_id if binding else "",
                "capacity_kw": binding.capacity_kw if binding else None,
                "dc_capacity_kw": binding.dc_capacity_kw if binding else None,
                "power_basis": binding.power_basis if binding else "ac",
                "installation_date": binding.installation_date if binding else None,
                "azimuth_degrees": binding.azimuth_degrees if binding else 0,
                "losses_percent": binding.losses_percent if binding else 0,
                "settings_verification_reference": (
                    binding.settings_verification_reference if binding else ""
                ),
            },
        )
        action_form = BindingActionForm(
            request.POST
            if request.method == "POST" and action != "configure" and not epias_action
            else None,
            initial={
                "expected_revision": region.revision,
                "binding_id": binding.pk if binding else None,
                "credential_revision": binding.provider_account.credential_revision
                if binding
                else None,
                "expected_cooldown_until": (
                    binding.provider_account.request_blocked_until.isoformat()
                    if binding and binding.provider_account.request_blocked_until is not None
                    else ""
                ),
            },
        )
        if request.method == "POST" and not epias_action:
            action_form.fields["provider_recovered"].required = action == "clear_cooldown"
            form = setup_form if action == "configure" else action_form
            if form.is_valid():
                try:
                    actor = cast(User, request.user)
                    if action == "configure":
                        saved = setup_form.configure(actor, region)
                        if saved.pk != setup_form.cleaned_data["expected_binding_id"]:
                            message = _(
                                "Private provider setup saved. "
                                "Validate the connection before approval."
                            )
                        elif setup_form.cleaned_data["api_key"]:
                            message = _(
                                "API key replaced. Validate the connection to resume collection."
                            )
                        else:
                            message = _("Nothing changed.")
                    elif action in {"validate", "approve", "clear_cooldown"}:
                        current = action_form.current_binding(region)
                        if action == "validate":
                            validate_binding(
                                actor, current.pk, adapter_for(current), timezone.now()
                            )
                            message = _(
                                "The current provider connection is validated. "
                                "The plant can now be approved."
                            )
                        elif action == "clear_cooldown":
                            clear_provider_cooldown(
                                actor,
                                current.provider_account_id,
                                action_form.cleaned_data["credential_revision"],
                                expected_blocked_until=action_form.cleaned_data[
                                    "expected_cooldown_until"
                                ],
                            )
                            message = _(
                                "Provider wait cleared. Queued collection can resume "
                                "within the remaining quota."
                            )
                        else:
                            approve_region(actor, region.pk, current.pk, timezone.now())
                            message = _(
                                "Plant approved. The first saved forecast is being prepared."
                            )
                    else:
                        raise ValidationError(_("Choose a setup action."))
                except RequestDeferredError as error:
                    form.add_error(
                        None,
                        _("Update the private API key before validating this connection again.")
                        if error.available_at is None
                        else _("The provider is limiting requests. Wait before validating again."),
                    )
                except QuotaExceededError:
                    form.add_error(
                        None,
                        _("The provider's request budget is unavailable. Try validation later."),
                    )
                except ProviderError as error:
                    form.add_error(None, validation_failure_message(error))
                except ObjectDoesNotExist, ValidationError, APIValidationError:
                    form.add_error(
                        None,
                        _(
                            "The setup could not be applied. Check the values or reload "
                            "if the plant or account changed."
                        ),
                    )
                else:
                    self.message_user(request, message)
                    return redirect("admin:workspace_plant", object_id=region.pk)
        submissions = PlantSubmission.objects.filter(plant__in=plants).select_related(
            "plant", "submitted_by"
        )
        return TemplateResponse(
            request,
            "admin/regions/region/setup.html",
            {
                **self.admin_site.each_context(request),
                **review_context(region.pk),
                "workspace_section": "plants",
                "title": _("Review and set up %(name)s") % {"name": region.name},
                "opts": self.opts,
                "region": region,
                "plants": plants,
                "submissions": submissions,
                "binding": binding,
                "active_provider_delay": bool(
                    binding
                    and binding.provider_account.request_blocked_until
                    and binding.provider_account.request_blocked_until > timezone.now()
                ),
                "setup_form": setup_form,
                "action_form": action_form,
                "epias_form": epias_form,
                "epias_connection": epias_connection,
                "can_connect_epias": bool(
                    plant
                    and plant.current_configuration
                    and configuration_is_current(plant.current_configuration)
                ),
                "can_approve": bool(
                    binding
                    and binding.validated_at
                    and binding.validated_credential_revision
                    == binding.provider_account.credential_revision
                    and binding.provider_account.enabled
                    and binding.provider_account.authorized_at is not None
                    and (
                        binding.provider_account.provider != "solcast"
                        or (binding.settings_verification_reference and binding.installation_date)
                    )
                ),
            },
            status=400 if request.method == "POST" else 200,
        )


@admin.register(RegionLocationChangeRequest, site=site)
class RegionLocationChangeRequestAdmin(ReadOnlyAdmin):
    list_display = ("id", "region", "status", "base_revision", "created_at", "reviewed_at")
    list_filter = ("status",)


for model in (
    Tenant,
    SiteBinding,
    CollectionJob,
    ForecastResult,
    RequestAttempt,
    AuditEvent,
    Plant,
    PlantConfiguration,
    ProductionObservation,
    CalibrationRun,
    CalibrationActivation,
    PlantEvent,
):
    site.register(model, ReadOnlyAdmin)
