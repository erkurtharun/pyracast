from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.db.models import Model, Q, QuerySet
from django.http import HttpRequest
from django.urls import URLPattern, reverse
from django.utils.html import format_html
from django.utils.safestring import SafeString
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from pyracast.accounts.models import Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    DispatcherStatus,
    ForecastResult,
    RequestAttempt,
)
from pyracast.notifications.models import NotificationDelivery, NotificationSettings, OutboundMail
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
    NotificationDelivery: active_region("binding__region__"),
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
    **dict.fromkeys(
        (
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
        ),
        ReadOnlyAdmin,
    ),
}


def register_records(site: admin.AdminSite) -> None:
    for model, model_admin in ADMINS.items():
        site.register(model, model_admin)
