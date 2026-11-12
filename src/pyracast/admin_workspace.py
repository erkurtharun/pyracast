from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, TypedDict, cast

from django import forms
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, OuterRef, Q, QuerySet, Subquery
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.accounts.operations import require_admin
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import CollectionJob, DispatcherStatus, ForecastSnapshot
from pyracast.forecasts.monitoring import delivery_health
from pyracast.notifications.models import NotificationDelivery, NotificationSettings
from pyracast.notifications.operations import recipient_limit
from pyracast.plants.models import Plant
from pyracast.regions.models import AuditEvent, Region, RegionLocationChangeRequest
from pyracast.regions.operations import review_location_change, transition_region

if TYPE_CHECKING:
    from django_stubs_ext import WithAnnotations

STATUS_CHOICES = (
    ("pending", "Awaiting approval"),
    ("approved", "Approved"),
    ("suspended", "Suspended"),
    ("rejected", "Rejected"),
)


@dataclass(frozen=True)
class PlantSummary:
    id: int
    name: str
    company: str
    plant_name: str
    status: str
    revision: int
    created_at: datetime
    latitude: float
    longitude: float
    timezone: str
    connection: str
    last_collected_at: datetime | None
    review_url: str


class WorkspaceAnnotations(TypedDict):
    workspace_plant_name: str | None
    workspace_binding_id: int | None
    workspace_binding_health: str | None
    workspace_validated_at: datetime | None
    workspace_validated_revision: int | None
    workspace_credential_revision: int | None
    workspace_account_enabled: bool | None
    workspace_authorized_at: datetime | None
    workspace_authentication_failed_revision: int | None
    workspace_request_blocked_until: datetime | None
    workspace_last_collected_at: datetime | None


def workspace_regions() -> QuerySet[WithAnnotations[Region, WorkspaceAnnotations]]:
    binding = SiteBinding.objects.filter(
        region_id=OuterRef("pk"), region_revision=OuterRef("revision")
    ).order_by("-created_at", "-pk")
    snapshot = ForecastSnapshot.objects.filter(
        binding_id=OuterRef("approved_binding_id"),
        source_kind="forecast",
        job__status="succeeded",
        forecastresult__isnull=False,
    ).order_by("-collected_at", "-pk")
    return (
        Region.objects.filter(deleted_at__isnull=True)
        .select_related("tenant")
        .annotate(
            workspace_plant_name=Subquery(
                Plant.objects.filter(region_id=OuterRef("pk"), deleted_at__isnull=True)
                .order_by("pk")
                .values("name")[:1]
            ),
            workspace_binding_id=Subquery(binding.values("pk")[:1]),
            workspace_binding_health=Subquery(binding.values("health")[:1]),
            workspace_validated_at=Subquery(binding.values("validated_at")[:1]),
            workspace_validated_revision=Subquery(
                binding.values("validated_credential_revision")[:1]
            ),
            workspace_credential_revision=Subquery(
                binding.values("provider_account__credential_revision")[:1]
            ),
            workspace_account_enabled=Subquery(binding.values("provider_account__enabled")[:1]),
            workspace_authorized_at=Subquery(binding.values("provider_account__authorized_at")[:1]),
            workspace_authentication_failed_revision=Subquery(
                binding.values("provider_account__authentication_failed_revision")[:1]
            ),
            workspace_request_blocked_until=Subquery(
                binding.values("provider_account__request_blocked_until")[:1]
            ),
            workspace_last_collected_at=Subquery(snapshot.values("collected_at")[:1]),
        )
        .order_by("-created_at", "-pk")
    )


def summarize_plant(region: WithAnnotations[Region, WorkspaceAnnotations]) -> PlantSummary:
    connection = "Healthy"
    if region.workspace_binding_id is None:
        connection = "Not configured"
    elif (
        not region.workspace_account_enabled
        or region.workspace_authorized_at is None
        or region.workspace_binding_health == "error"
        or region.workspace_authentication_failed_revision == region.workspace_credential_revision
    ):
        connection = "Needs attention"
    elif (
        region.workspace_request_blocked_until is not None
        and region.workspace_request_blocked_until > timezone.now()
    ):
        connection = "Provider delay"
    elif (
        region.workspace_validated_at is None
        or region.workspace_validated_revision != region.workspace_credential_revision
    ):
        connection = "Needs validation"
    return PlantSummary(
        id=region.pk,
        name=region.name,
        company=region.tenant.name,
        plant_name=region.workspace_plant_name or region.name,
        status=region.status,
        revision=region.revision,
        created_at=region.created_at,
        latitude=region.latitude,
        longitude=region.longitude,
        timezone=region.timezone,
        connection=connection,
        last_collected_at=region.workspace_last_collected_at,
        review_url=reverse("admin:workspace_plant", args=(region.pk,)),
    )


def plant_list_context(query: str, status: str, page: str) -> dict[str, Any]:
    regions = workspace_regions()
    if query:
        regions = regions.filter(
            Q(name__icontains=query)
            | Q(tenant__name__icontains=query)
            | Q(plant__name__icontains=query, plant__deleted_at__isnull=True)
        ).distinct()
    if status in dict(STATUS_CHOICES):
        regions = regions.filter(status=status)
    else:
        status = ""
    page_obj = Paginator(regions, 20).get_page(page)
    return {
        "plants": [summarize_plant(region) for region in page_obj],
        "page_obj": page_obj,
        "q": query,
        "status": status,
        "status_choices": STATUS_CHOICES,
    }


def overview_context() -> dict[str, Any]:
    regions = workspace_regions()
    counts = {status: 0 for status, _ in STATUS_CHOICES}
    counts.update(
        {
            row["status"]: row["count"]
            for row in regions.values("status").annotate(count=Count("pk")).order_by()
        }
    )
    counts["total"] = sum(counts.values())
    counts["location_changes"] = RegionLocationChangeRequest.objects.filter(
        status="pending", region__deleted_at__isnull=True
    ).count()
    return {
        "counts": counts,
        "review_plants": [
            summarize_plant(region)
            for region in regions.filter(status="pending").order_by("created_at", "pk")[:6]
        ],
        "health": delivery_health(timezone.now(), timedelta(minutes=5)),
        "location_change_plants": [
            {
                "id": change.pk,
                "name": change.region.name,
                "company": change.region.tenant.name,
                "created_at": change.created_at,
                "review_url": reverse("admin:workspace_plant", args=(change.region_id,)),
            }
            for change in RegionLocationChangeRequest.objects.filter(
                status="pending", region__deleted_at__isnull=True
            )
            .select_related("region__tenant")
            .order_by("created_at", "pk")[:6]
        ],
        "recent_activity": recent_activity_context(limit=8),
    }


def recent_activity_context(
    region_id: int | None = None, *, limit: int = 5
) -> list[dict[str, Any]]:
    events = AuditEvent.objects.filter(region__deleted_at__isnull=True, region__isnull=False)
    if region_id is not None:
        events = events.filter(region_id=region_id)
    labels = {
        "approved": "Plant approved",
        "rejected": "Plant rejected",
        "suspended": "Plant suspended",
        "binding_configured": "Provider connection configured",
        "binding_validated": "Provider connection validated",
        "location_change_approved": "Location change approved",
        "location_change_rejected": "Location change rejected",
    }
    return [
        {
            **row,
            "action_label": labels.get(row["action"], row["action"].replace("_", " ").capitalize()),
        }
        for row in events.order_by("-created_at", "-pk").values(
            "action", "created_at", "region__name", "actor__username"
        )[:limit]
    ]


def recent_work_context(region_id: int | None = None, limit: int = 20) -> dict[str, Any]:
    collection_rows = CollectionJob.objects.filter(
        binding__region__deleted_at__isnull=True
    ).annotate(
        display_plant_name=Coalesce(
            Subquery(
                Plant.objects.filter(
                    region_id=OuterRef("binding__region_id"), deleted_at__isnull=True
                )
                .order_by("pk")
                .values("name")[:1]
            ),
            "binding__region__name",
        )
    )
    delivery_rows = NotificationDelivery.objects.filter(
        preference__region__deleted_at__isnull=True
    ).annotate(
        display_plant_name=Coalesce(
            Subquery(
                Plant.objects.filter(
                    region_id=OuterRef("preference__region_id"), deleted_at__isnull=True
                )
                .order_by("pk")
                .values("name")[:1]
            ),
            "preference__region__name",
        )
    )
    if region_id is not None:
        collection_rows = collection_rows.filter(binding__region_id=region_id)
        delivery_rows = delivery_rows.filter(preference__region_id=region_id)
    collections = [
        {
            "id": row.pk,
            "plant_name": row.display_plant_name,
            "company": row.binding.region.tenant.name,
            "status": row.status,
            "kind": "Forecast" if row.endpoint == "forecasts" else "Past estimates",
            "target_date": row.target_date,
            "status_label": row.status.replace("_", " ").capitalize(),
            "scheduled_for": row.scheduled_slot,
            "last_error": row.last_error,
            "error_label": row.last_error.replace("_", " ").capitalize(),
            "attempts": row.attempts,
            "review_url": reverse("admin:workspace_plant", args=(row.binding.region_id,)),
        }
        for row in collection_rows.select_related("binding__region__tenant").order_by(
            "-created_at", "-pk"
        )[:limit]
    ]
    deliveries = [
        {
            "id": row.pk,
            "plant_name": row.display_plant_name,
            "company": row.preference.region.tenant.name,
            "recipient": row.recipient,
            "status": row.status,
            "status_label": {
                "sent": "Accepted by email service",
                "uncertain": "Acceptance unknown",
            }.get(row.status, row.status.replace("_", " ").capitalize()),
            "scheduled_for": row.scheduled_for,
            "sent_at": row.sent_at,
            "last_error": row.last_error,
            "error_label": row.last_error.replace("_", " ").capitalize(),
            "attempts": row.attempts,
            "review_url": reverse("admin:workspace_plant", args=(row.preference.region_id,)),
        }
        for row in delivery_rows.select_related("preference__region__tenant").order_by(
            "-scheduled_for", "-pk"
        )[:limit]
    ]
    return {"collections": collections, "deliveries": deliveries}


def operations_context() -> dict[str, Any]:
    health = delivery_health(timezone.now(), timedelta(minutes=5))
    checks = (
        (
            "invalid_reminder_recipients",
            "Invalid email recipients",
            "Ask the member to correct the saved recipient addresses.",
        ),
        (
            "unvalidated_provider_credentials",
            "Provider keys awaiting validation",
            "Validate the current private key before collections resume.",
        ),
        (
            "interrupted_notifications",
            "Interrupted emails",
            "Review the outcome; uncertain transmission must not be resent automatically.",
        ),
        (
            "missed_notifications",
            "Missed email times",
            "Check notification worker availability and the delivery deadline.",
        ),
        (
            "overdue_metering",
            "Delayed production collection",
            "Check the production dispatcher and worker.",
        ),
        (
            "interrupted_metering",
            "Interrupted production collection",
            "The durable dispatcher recovers expired leases.",
        ),
        (
            "unavailable_forecasts",
            "Emails without saved forecasts",
            "Review the plant collection history and approved connection.",
        ),
        (
            "partial_forecasts",
            "Emails with partial forecasts",
            "Review saved forecast coverage; missing intervals are not zero.",
        ),
        (
            "overdue_collections",
            "Delayed collections",
            "Check the collection dispatcher and worker.",
        ),
        (
            "interrupted_collections",
            "Interrupted collections",
            "The durable dispatcher recovers expired leases.",
        ),
        (
            "failed_collections",
            "Failed collections",
            "Review the plant connection and recorded error.",
        ),
        (
            "regions_without_recent_forecasts",
            "Plants without recent forecasts",
            "Review saved acquisition times and collection settings.",
        ),
        (
            "blocked_provider_credentials",
            "Provider keys needing attention",
            "Replace the private key, then validate the connection.",
        ),
        (
            "provider_cooldowns",
            "Provider request delays",
            "Wait for provider recovery; preserve the shared daily quota.",
        ),
        (
            "failed_notifications",
            "Email needing attention",
            "Uncertain transmission must be reconciled before any manual resend.",
        ),
        ("overdue_notifications", "Delayed emails", "Check the dedicated notification worker."),
        (
            "mail_authentication_failures",
            "Email authentication failures",
            "Check deployment mail credentials.",
        ),
        (
            "failed_metering",
            "Failed production collection",
            "Review the reported-production connection.",
        ),
    )
    phases = {row.phase: row for row in DispatcherStatus.objects.all()}
    unhealthy_phases = cast(list[str], health["unhealthy_dispatch_phases"])
    return {
        "health": health,
        "health_checks": [
            {"key": key, "label": label, "count": health[key], "help": help_text}
            for key, label, help_text in checks
        ],
        "dispatch_phases": [
            {
                "label": label,
                "attempted_at": phases[name].attempted_at if name in phases else None,
                "succeeded_at": phases[name].succeeded_at if name in phases else None,
                "last_error": phases[name].last_error if name in phases else "",
                "unhealthy": name in unhealthy_phases,
                "state_label": "Needs attention" if name in unhealthy_phases else "Current",
            }
            for name, label in DispatcherStatus.Phase.choices
        ],
        **recent_work_context(),
    }


class WorkspaceSettingsForm(forms.Form):
    max_recipients = forms.IntegerField(min_value=1, max_value=32767, label="Recipients per plant")
    expected_max_recipients = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    def apply(self, actor: User) -> None:
        require_admin(actor)
        with transaction.atomic():
            settings, _ = NotificationSettings.objects.select_for_update().get_or_create(pk=1)
            if settings.max_recipients != self.cleaned_data["expected_max_recipients"]:
                raise ValidationError("Settings changed. Reload before saving.")
            value = self.cleaned_data["max_recipients"]
            if value == settings.max_recipients:
                return
            previous = settings.max_recipients
            settings.max_recipients = value
            settings.full_clean()
            settings.save(update_fields=("max_recipients",))
            LogEntry.objects.create(
                user=actor,
                content_type=ContentType.objects.get_for_model(NotificationSettings),
                object_id=str(settings.pk),
                object_repr="Email reminder settings",
                action_flag=CHANGE,
                change_message=f"Recipient limit changed from {previous} to {value}.",
            )


class ReviewDecisionForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_status = forms.ChoiceField(choices=STATUS_CHOICES, widget=forms.HiddenInput)
    location_change_id = forms.IntegerField(min_value=1, required=False, widget=forms.HiddenInput)
    action = forms.ChoiceField(
        choices=tuple(
            (name, name) for name in ("reject", "suspend", "approve_location", "reject_location")
        ),
        widget=forms.HiddenInput,
    )

    def apply(self, actor: User, region_id: int) -> None:
        require_admin(actor)
        data = self.cleaned_data
        with transaction.atomic():
            if data["action"] == "approve_location":
                tenant_id = Region.objects.values_list("tenant_id", flat=True).get(
                    pk=region_id, deleted_at__isnull=True
                )
                Tenant.objects.select_for_update().get(pk=tenant_id)
            region = Region.objects.select_for_update().get(pk=region_id, deleted_at__isnull=True)
            if (
                region.revision != data["expected_revision"]
                or region.status != data["expected_status"]
            ):
                raise ValidationError("The plant changed. Reload before reviewing it.")
            if data["action"] in {"reject", "suspend"}:
                required_status = "pending" if data["action"] == "reject" else "approved"
                if region.status != required_status:
                    raise ValidationError(
                        "This decision is unavailable for the current review status."
                    )
                transition_region(
                    actor, region.pk, "rejected" if data["action"] == "reject" else "suspended"
                )
            else:
                change = RegionLocationChangeRequest.objects.get(
                    pk=data["location_change_id"], region=region, status="pending"
                )
                review_location_change(
                    actor, change.pk, approve=data["action"] == "approve_location"
                )


def review_context(region_id: int) -> dict[str, Any]:
    region = workspace_regions().get(pk=region_id)
    summary = summarize_plant(region)
    work = recent_work_context(region.pk, limit=5)
    return {
        "plant_summary": summary,
        "last_collected_at": summary.last_collected_at,
        "recent_collections": work["collections"],
        "recent_deliveries": work["deliveries"],
        "recent_activity": recent_activity_context(region.pk),
        "review_form": ReviewDecisionForm(
            initial={"expected_revision": region.revision, "expected_status": region.status}
        ),
        "location_changes": [
            {
                "id": change.pk,
                "latitude": change.latitude,
                "longitude": change.longitude,
                "timezone": change.timezone,
                "reason": change.reason,
                "created_at": change.created_at,
                "base_revision": change.base_revision,
                "requested_by": change.requested_by.username,
            }
            for change in RegionLocationChangeRequest.objects.filter(
                region=region, status="pending"
            )
            .select_related("requested_by")
            .order_by("created_at", "pk")
        ],
    }


def settings_context(data: Any = None) -> dict[str, Any]:
    limit = recipient_limit()
    return {
        "recipient_limit": limit,
        "settings_form": WorkspaceSettingsForm(
            data, initial={"max_recipients": limit, "expected_max_recipients": limit}
        ),
    }
