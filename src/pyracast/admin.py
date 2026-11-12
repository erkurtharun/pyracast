from typing import TYPE_CHECKING, Any, cast

from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import URLPattern, URLResolver, path, reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.utils.safestring import SafeString
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from rest_framework.exceptions import ValidationError as APIValidationError

from pyracast.accounts.models import Tenant
from pyracast.accounts.security import has_verified_mfa
from pyracast.admin_forms import BindingActionForm, ProviderSetupForm
from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import SiteBinding
from pyracast.connections.operations import (
    QuotaExceededError,
    RequestDeferredError,
    validate_binding,
)
from pyracast.forecasts.models import (
    CollectionJob,
    DispatcherStatus,
    ForecastResult,
    RequestAttempt,
)
from pyracast.notifications.models import NotificationDelivery, NotificationSettings
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
    PlantForecast,
    PlantSubmission,
    ProductionObservation,
)
from pyracast.providers import adapter_for
from pyracast.regions.models import AuditEvent, Region, RegionLocationChangeRequest
from pyracast.regions.operations import approve_region, review_location_change, transition_region


class PlatformAdminSite(admin.AdminSite):
    def has_permission(self, request: HttpRequest) -> bool:
        return bool(
            super().has_permission(request)
            and request.user.is_superuser
            and has_verified_mfa(request)
        )

    def login(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        if self.has_permission(request):
            return redirect("admin:index")
        return redirect("/?next=/admin/")

    def get_urls(self) -> list[URLPattern | URLResolver]:
        return [
            path(
                "password_change/", self.admin_view(self.account_security), name="password_change"
            ),
            *[
                url
                for url in super().get_urls()
                if not isinstance(url, URLPattern) or url.name != "password_change"
            ],
        ]

    def account_security(self, request: HttpRequest) -> HttpResponse:
        return redirect("/?account=security")


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


@admin.register(NotificationSettings, site=site)
class NotificationSettingsAdmin(AdminBase):
    fields = ("max_recipients",)
    list_display = ("max_recipients",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return not NotificationSettings.objects.exists()

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(DispatcherStatus, site=site)
class DispatcherStatusAdmin(ReadOnlyAdmin):
    list_display = ("phase", "attempted_at", "succeeded_at", "last_error")


@admin.register(NotificationDelivery, site=site)
class NotificationDeliveryAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "recipient",
        "scheduled_for",
        "status",
        "attempts",
        "last_error",
        "sent_at",
    )
    list_filter = ("status",)
    search_fields = ("recipient", "preference__region__name")
    fields = (
        "preference",
        "recipient",
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
    actions = ("approve", "reject", "suspend")

    def get_urls(self) -> list[URLPattern]:
        return [
            path(
                "<int:object_id>/setup/",
                self.admin_site.admin_view(self.setup),
                name="regions_region_setup",
            ),
            *super().get_urls(),
        ]

    @admin.display(description="Onboarding")
    def review_link(self, obj: Region) -> SafeString:
        return format_html(
            '<a href="{}">Review and set up</a>',
            reverse("admin:regions_region_setup", args=(obj.pk,)),
        )

    @method_decorator(sensitive_post_parameters("api_key"))
    @sensitive_variables()
    def setup(self, request: HttpRequest, object_id: int) -> HttpResponse:
        region = get_object_or_404(
            Region.objects.select_related("tenant"), pk=object_id, deleted_at__isnull=True
        )
        binding = (
            SiteBinding.objects.select_related("provider_account")
            .filter(region=region, region_revision=region.revision)
            .order_by("-created_at", "-pk")
            .first()
        )
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
            if request.method == "POST" and request.POST.get("action") != "configure"
            else None,
            initial={
                "expected_revision": region.revision,
                "binding_id": binding.pk if binding else None,
                "credential_revision": binding.provider_account.credential_revision
                if binding
                else None,
            },
        )
        if request.method == "POST":
            action = request.POST.get("action")
            form = setup_form if action == "configure" else action_form
            if form.is_valid():
                try:
                    actor = cast(User, request.user)
                    if action == "configure":
                        setup_form.configure(actor, region)
                        message = (
                            "Private provider setup saved. Validate the connection before approval."
                        )
                    elif action in {"validate", "approve"}:
                        current = action_form.current_binding(region)
                        if action == "validate":
                            validate_binding(
                                actor, current.pk, adapter_for(current), timezone.now()
                            )
                            message = (
                                "The current provider connection is validated. "
                                "The plant can now be approved."
                            )
                        else:
                            approve_region(actor, region.pk, current.pk, timezone.now())
                            message = "Plant approved. The first saved forecast is being prepared."
                    else:
                        raise ValidationError("Choose a setup action.")
                except RequestDeferredError as error:
                    form.add_error(
                        None,
                        "Update the private API key before validating this connection again."
                        if error.available_at is None
                        else "The provider is limiting requests. Wait before validating again.",
                    )
                except QuotaExceededError:
                    form.add_error(
                        None, "The provider's request budget is unavailable. Try validation later."
                    )
                except ProviderError:
                    form.add_error(
                        None,
                        "Connection validation failed. Check the private credentials "
                        "and site settings.",
                    )
                except ObjectDoesNotExist, ValidationError, APIValidationError:
                    form.add_error(
                        None,
                        "The setup could not be applied. Check the values or reload "
                        "if the plant or account changed.",
                    )
                else:
                    self.message_user(request, message)
                    return redirect("admin:regions_region_setup", object_id=region.pk)
        plants = Plant.objects.filter(region=region, deleted_at__isnull=True)
        submissions = PlantSubmission.objects.filter(plant__in=plants).select_related(
            "plant", "submitted_by"
        )
        return TemplateResponse(
            request,
            "admin/regions/region/setup.html",
            {
                **self.admin_site.each_context(request),
                "title": f"Review and set up {region.name}",
                "opts": self.model._meta,
                "region": region,
                "plants": plants,
                "submissions": submissions,
                "binding": binding,
                "setup_form": setup_form,
                "action_form": action_form,
                "can_approve": bool(
                    binding
                    and binding.validated_at
                    and binding.validated_credential_revision
                    == binding.provider_account.credential_revision
                    and binding.provider_account.enabled
                ),
            },
            status=400 if request.method == "POST" else 200,
        )

    @admin.action(description="Approve validated reference binding", permissions=["view"])
    def approve(self, request: HttpRequest, queryset: QuerySet[Region]) -> None:
        for region in queryset.filter(deleted_at__isnull=True):
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
        for region in queryset.filter(deleted_at__isnull=True):
            transition_region(cast(User, request.user), region.pk, "rejected")

    @admin.action(description="Suspend region", permissions=["view"])
    def suspend(self, request: HttpRequest, queryset: QuerySet[Region]) -> None:
        for region in queryset.filter(deleted_at__isnull=True):
            transition_region(cast(User, request.user), region.pk, "suspended")


@admin.register(RegionLocationChangeRequest, site=site)
class RegionLocationChangeRequestAdmin(ReadOnlyAdmin):
    list_display = ("id", "region", "status", "base_revision", "created_at", "reviewed_at")
    list_filter = ("status",)
    actions = ("approve", "reject")

    def review(
        self,
        request: HttpRequest,
        queryset: QuerySet[RegionLocationChangeRequest],
        *,
        approve: bool,
    ) -> None:
        for change in queryset.filter(region__deleted_at__isnull=True):
            try:
                review_location_change(cast(User, request.user), change.pk, approve=approve)
            except ObjectDoesNotExist, ValidationError:
                self.message_user(
                    request,
                    "The request is stale or already reviewed; review the current region.",
                    level="ERROR",
                )

    @admin.action(
        description="Approve location change (new binding validation and approval required)",
        permissions=["view"],
    )
    def approve(
        self, request: HttpRequest, queryset: QuerySet[RegionLocationChangeRequest]
    ) -> None:
        self.review(request, queryset, approve=True)

    @admin.action(description="Reject location change", permissions=["view"])
    def reject(self, request: HttpRequest, queryset: QuerySet[RegionLocationChangeRequest]) -> None:
        self.review(request, queryset, approve=False)


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
    PlantForecast,
    PlantEvent,
):
    site.register(model, ReadOnlyAdmin)
