from typing import TYPE_CHECKING, Any, cast

from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone
from django_otp.admin import OTPAdminSite

from pyracast.accounts.models import Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import CollectionJob, ForecastResult, RequestAttempt
from pyracast.regions.models import AuditEvent, Region
from pyracast.regions.operations import approve_region, transition_region


class PlatformAdminSite(admin.AdminSite):
    login_form = OTPAdminSite.login_form
    login_template = OTPAdminSite.login_template

    def has_permission(self, request: HttpRequest) -> bool:
        return bool(
            super().has_permission(request)
            and request.user.is_superuser
            and getattr(request.user, "is_verified", lambda: False)()
        )


site = PlatformAdminSite(name="admin")
site.site_header = "Pyracast operations"


if TYPE_CHECKING:
    AdminBase = admin.ModelAdmin[Any]
else:
    AdminBase = admin.ModelAdmin


class ReadOnlyAdmin(AdminBase):
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Region, site=site)
class RegionAdmin(ReadOnlyAdmin):
    list_display = ("id", "name", "tenant", "status", "revision")
    list_filter = ("status",)
    actions = ("approve", "reject", "suspend")

    @admin.action(description="Approve validated reference binding", permissions=["view"])
    def approve(self, request: HttpRequest, queryset: QuerySet[Region]) -> None:
        for region in queryset:
            try:
                binding = SiteBinding.objects.filter(
                    region=region, region_revision=region.revision, validated_at__isnull=False
                ).latest("created_at")
                approve_region(cast(User, request.user), region.pk, binding.pk, timezone.now())
            except ObjectDoesNotExist, ValidationError:
                self.message_user(
                    request, "Validate the current binding before approval.", level="ERROR"
                )

    @admin.action(description="Reject region", permissions=["view"])
    def reject(self, request: HttpRequest, queryset: QuerySet[Region]) -> None:
        for region in queryset:
            transition_region(cast(User, request.user), region.pk, "rejected")

    @admin.action(description="Suspend region", permissions=["view"])
    def suspend(self, request: HttpRequest, queryset: QuerySet[Region]) -> None:
        for region in queryset:
            transition_region(cast(User, request.user), region.pk, "suspended")


for model in (Tenant, SiteBinding, CollectionJob, ForecastResult, RequestAttempt, AuditEvent):
    site.register(model, ReadOnlyAdmin)
