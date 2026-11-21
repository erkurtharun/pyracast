from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.db.models import Model, Q, QuerySet
from django.http import HttpRequest
from django.urls import URLPattern, reverse
from django.utils.html import format_html
from django.utils.safestring import SafeString
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from pyracast.accounts.models import (
    OutboundMail,
    Tenant,
    WorkspaceAdminChange,
    WorkspaceMemberRemoval,
)
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    DispatcherStatus,
    ForecastResult,
    RequestAttempt,
)
from pyracast.notifications.models import NotificationDelivery, NotificationSettings
from pyracast.notifications.recipients import delivery_addresses
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
    ProductionObservation,
    active_plant,
)
from pyracast.regions.models import AuditEvent, Region, RegionLocationChangeRequest, active_region

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

    AdminBase = admin.ModelAdmin[Any]
else:
    AdminBase = admin.ModelAdmin

# Records of a plant location in the trash stay out of the record browser, as they stay out of
# every console page; a nullable link (an account-level attempt or event) keeps its row.
LIVE_ROWS: dict[type[Model], Q] = {
    Region: active_region(),
    RegionLocationChangeRequest: active_region("region__"),
    AuditEvent: active_region("region__"),
    SiteBinding: active_region("region__"),
    CollectionJob: active_region("binding__region__"),
    RequestAttempt: active_region("binding__region__"),
    ForecastResult: active_region("snapshot__binding__region__"),
    NotificationDelivery: active_region("schedule__region__"),
    Plant: active_plant(),
    PlantConfiguration: active_plant("plant__"),
    ProductionObservation: active_plant("plant__"),
    CalibrationRun: active_plant("plant__"),
    CalibrationActivation: active_plant("plant__"),
    PlantEvent: active_plant("plant__"),
}


class ReadOnlyAdmin(AdminBase):
    def get_urls(self) -> list[URLPattern]:
        prefix = f"{self.opts.app_label}_{self.opts.model_name}_"
        kept = {prefix + view for view in ("changelist", "history", "change")}
        return [url for url in super().get_urls() if url.name in kept]

    def get_queryset(self, request: HttpRequest) -> QuerySet[Any]:
        return super().get_queryset(request).filter(LIVE_ROWS.get(self.model, Q()))

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


def region_filter(path: str) -> type[admin.SimpleListFilter]:
    """A filter by plant location for the plant page's record links; it shows only the
    location the link names, and a value naming no live location filters nothing."""

    class RegionFilter(admin.SimpleListFilter):
        title = gettext_lazy("plant location")
        parameter_name = "region"

        def _region(self) -> tuple[int, str] | None:
            value = self.value()
            if not value or not value.isdigit():
                return None
            return Region.objects.active().filter(pk=int(value)).values_list("pk", "name").first()

        def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
            region = self._region()
            return [(str(region[0]), region[1])] if region else []

        def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
            return queryset.filter(**{path: self.value()})

    return RegionFilter


def region_records(region_id: int) -> list[tuple[StrOrPromise, str]]:
    """Links from a plant page to its rows in the record browser."""
    return [
        (label, f"{reverse(f'admin:{name}_changelist')}?region={region_id}")
        for label, name in (
            (gettext_lazy("Audit trail"), "regions_auditevent"),
            (gettext_lazy("Plant events"), "plants_plantevent"),
            (gettext_lazy("Collection jobs"), "forecasts_collectionjob"),
            (gettext_lazy("Request attempts"), "forecasts_requestattempt"),
            (gettext_lazy("Email deliveries"), "notifications_notificationdelivery"),
            (gettext_lazy("Provider bindings"), "connections_sitebinding"),
        )
    ]


class NotificationSettingsAdmin(ReadOnlyAdmin):
    fields = ("max_recipients",)
    list_display = ("max_recipients",)


class DispatcherStatusAdmin(ReadOnlyAdmin):
    list_display = ("phase", "attempted_at", "succeeded_at", "last_error")


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
    list_filter = ("status", region_filter("schedule__region"))
    list_select_related = ("schedule__region",)
    search_fields = ("recipient_rows__key", "schedule__region__name")
    readonly_fields = ("recipients",)
    ordering = ("-scheduled_for", "-pk")
    show_full_result_count = False
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

    def get_queryset(self, request: HttpRequest) -> QuerySet[NotificationDelivery]:
        return (
            super()
            .get_queryset(request)
            .defer("prepared_message")
            .prefetch_related("recipient_rows")
        )

    @admin.display(description=gettext_lazy("Recipients"))
    def recipients(self, obj: NotificationDelivery) -> str:
        return ", ".join(delivery_addresses(obj))


class OutboundMailAdmin(ReadOnlyAdmin):
    list_display = ("id", "kind", "status", "attempts", "last_error", "created_at", "finished_at")
    list_filter = ("status", "kind")
    fields = (
        "kind",
        "recipient",
        "status",
        "attempts",
        "last_error",
        "created_at",
        "available_at",
        "expires_at",
        "transmit_started_at",
        "finished_at",
    )


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


class AuditEventAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "action", "region", "provider_account", "actor", "revision")
    list_filter = ("action", region_filter("region"))
    list_select_related = ("region", "provider_account", "actor")
    search_fields = ("region__name", "provider_account__external_account_id")
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-pk")
    show_full_result_count = False


class PlantEventAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "action", "plant", "actor")
    list_filter = ("action", region_filter("plant__region"))
    list_select_related = ("plant", "actor")
    search_fields = ("plant__name",)
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-pk")
    show_full_result_count = False


class PlantAdmin(ReadOnlyAdmin):
    list_display = ("name", "region", "automatic_calibration_enabled")
    list_select_related = ("region",)
    search_fields = ("name", "region__name", "region__tenant__name")
    ordering = ("name", "pk")
    show_full_result_count = False


class SiteBindingAdmin(ReadOnlyAdmin):
    list_display = ("id", "region", "provider_account", "site_id", "region_revision", "health")
    list_filter = ("health", region_filter("region"))
    list_select_related = ("region", "provider_account")
    search_fields = ("site_id", "region__name")
    ordering = ("-pk",)
    show_full_result_count = False


class CollectionJobAdmin(ReadOnlyAdmin):
    list_display = ("id", "binding", "endpoint", "status", "scheduled_for", "attempts")
    list_filter = ("status", "endpoint", region_filter("binding__region"))
    list_select_related = ("binding",)
    date_hierarchy = "scheduled_for"
    ordering = ("-scheduled_for", "-pk")
    show_full_result_count = False


class RequestAttemptAdmin(ReadOnlyAdmin):
    list_display = ("id", "provider_account", "binding", "purpose", "outcome", "created_at")
    list_filter = ("purpose", "outcome", region_filter("binding__region"))
    list_select_related = ("provider_account", "binding")
    ordering = ("-created_at", "-pk")
    show_full_result_count = False


class WorkspaceAdminChangeAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "tenant", "actor", "administrator", "previous_administrator")
    list_select_related = ("tenant", "actor", "administrator", "previous_administrator")
    search_fields = ("tenant__name",)
    ordering = ("-created_at", "-pk")


class WorkspaceMemberRemovalAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "tenant", "actor", "user")
    list_select_related = ("tenant", "actor", "user")
    search_fields = ("tenant__name",)
    ordering = ("-created_at", "-pk")


class RegionLocationChangeRequestAdmin(ReadOnlyAdmin):
    list_display = ("id", "region", "status", "base_revision", "created_at", "reviewed_at")
    list_filter = ("status",)


ADMINS: dict[type[Model], type[ReadOnlyAdmin]] = {
    NotificationSettings: NotificationSettingsAdmin,
    DispatcherStatus: DispatcherStatusAdmin,
    NotificationDelivery: NotificationDeliveryAdmin,
    OutboundMail: OutboundMailAdmin,
    Region: RegionAdmin,
    RegionLocationChangeRequest: RegionLocationChangeRequestAdmin,
    AuditEvent: AuditEventAdmin,
    PlantEvent: PlantEventAdmin,
    Plant: PlantAdmin,
    SiteBinding: SiteBindingAdmin,
    CollectionJob: CollectionJobAdmin,
    RequestAttempt: RequestAttemptAdmin,
    WorkspaceAdminChange: WorkspaceAdminChangeAdmin,
    WorkspaceMemberRemoval: WorkspaceMemberRemovalAdmin,
    **dict.fromkeys(
        (
            Tenant,
            ForecastResult,
            PlantConfiguration,
            ProductionObservation,
            CalibrationRun,
            CalibrationActivation,
        ),
        ReadOnlyAdmin,
    ),
}


def register_records(site: admin.AdminSite) -> None:
    for model, model_admin in ADMINS.items():
        site.register(model, model_admin)
