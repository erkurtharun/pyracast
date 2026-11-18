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
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from pyracast.accounts.authorization import require_platform_admin
from pyracast.accounts.models import Tenant
from pyracast.admin_health import summarize_health
from pyracast.admin_health_queries import (
    collection_jobs_for_issue,
    metering_jobs_for_issue,
    notification_deliveries_for_issue,
    regions_for_issue,
)
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJobStatus,
    DispatcherPhase,
    DispatcherStatus,
    ForecastSnapshot,
    SnapshotSourceKind,
)
from pyracast.forecasts.monitoring import delivery_health
from pyracast.metering.models import EpiasJobError, EpiasJobStatus
from pyracast.notifications.models import NotificationSettings, PreparedMessageKind
from pyracast.notifications.operations import recipient_limit
from pyracast.plants.models import Plant
from pyracast.regions.models import (
    AuditAction,
    AuditEvent,
    LocationChangeStatus,
    Region,
    RegionLocationChangeRequest,
    RegionStatus,
    active_region,
)
from pyracast.regions.operations import review_location_change, transition_region

if TYPE_CHECKING:
    from django_stubs_ext import WithAnnotations


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
    )
    snapshot = ForecastSnapshot.objects.filter(
        binding_id=OuterRef("approved_binding_id"),
        source_kind=SnapshotSourceKind.FORECAST,
        job__status=CollectionJobStatus.SUCCEEDED,
        forecastresult__isnull=False,
    ).order_by("-collected_at", "-pk")
    return (
        Region.objects.active()
        .select_related("tenant")
        .annotate(
            workspace_plant_name=Subquery(
                Plant.objects.filter(region_id=OuterRef("pk")).order_by("pk").values("name")[:1]
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


def plant_list_context(query: str, status: str, page: str, issue: str = "") -> dict[str, Any]:
    regions = workspace_regions()
    health_filter = None
    if issue:
        now = timezone.now()
        summary = summarize_health(delivery_health(now, timedelta(minutes=5)))
        health_filter = next((check for check in summary.checks if check.key == issue), None)
        if health_filter:
            regions = regions.filter(pk__in=regions_for_issue(issue, now).values("pk"))
    if query:
        regions = regions.filter(
            Q(name__icontains=query)
            | Q(tenant__name__icontains=query)
            | Q(plant__name__icontains=query)
        ).distinct()
    if status in RegionStatus.values:
        regions = regions.filter(status=status)
    else:
        status = ""
    page_obj = Paginator(regions, 20).get_page(page)
    return {
        "plants": [summarize_plant(region) for region in page_obj],
        "page_obj": page_obj,
        "q": query,
        "status": status,
        "status_choices": RegionStatus.choices,
        "health_filter": health_filter,
    }


def overview_context() -> dict[str, Any]:
    regions = workspace_regions()
    health = delivery_health(timezone.now(), timedelta(minutes=5))
    counts = dict.fromkeys(RegionStatus.values, 0)
    counts.update(
        {
            row["status"]: row["count"]
            for row in regions.values("status").annotate(count=Count("pk")).order_by()
        }
    )
    counts["total"] = sum(counts.values())
    counts["location_changes"] = RegionLocationChangeRequest.objects.filter(
        active_region("region__"), status=LocationChangeStatus.PENDING
    ).count()
    return {
        "counts": counts,
        "review_plants": [
            summarize_plant(region)
            for region in regions.filter(status=RegionStatus.PENDING).order_by("created_at", "pk")[
                :6
            ]
        ],
        "health": health,
        "health_summary": summarize_health(health),
        "location_change_plants": [
            {
                "id": change.pk,
                "name": change.region.name,
                "company": change.region.tenant.name,
                "created_at": change.created_at,
                "review_url": reverse("admin:workspace_plant", args=(change.region_id,)),
            }
            for change in RegionLocationChangeRequest.objects.filter(
                active_region("region__"), status=LocationChangeStatus.PENDING
            )
            .select_related("region__tenant")
            .order_by("created_at", "pk")[:6]
        ],
        "recent_activity": recent_activity_context(limit=8),
    }


def recent_activity_context(
    region_id: int | None = None, *, limit: int = 5
) -> list[dict[str, Any]]:
    events = AuditEvent.objects.filter(active_region("region__"), region__isnull=False)
    if region_id is not None:
        events = events.filter(region_id=region_id)
    return [
        {**row, "action_label": str(AuditAction(row["action"]).label)}
        for row in events.order_by("-created_at", "-pk").values(
            "action", "created_at", "region__name", "actor__username"
        )[:limit]
    ]


def recent_work_context(
    region_id: int | None = None,
    limit: int = 20,
    *,
    issue: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or timezone.now()
    collection_rows = collection_jobs_for_issue(issue, now).annotate(
        display_plant_name=Coalesce(
            Subquery(
                Plant.objects.filter(region_id=OuterRef("binding__region_id"))
                .order_by("pk")
                .values("name")[:1]
            ),
            "binding__region__name",
        )
    )
    delivery_rows = notification_deliveries_for_issue(issue, now).annotate(
        display_plant_name=Coalesce(
            Subquery(
                Plant.objects.filter(region_id=OuterRef("schedule__region_id"))
                .order_by("pk")
                .values("name")[:1]
            ),
            "schedule__region__name",
        )
    )
    metering_rows = metering_jobs_for_issue(issue, now)
    if region_id is not None:
        collection_rows = collection_rows.filter(binding__region_id=region_id)
        delivery_rows = delivery_rows.filter(schedule__region_id=region_id)
        metering_rows = metering_rows.filter(plant__region_id=region_id)
    collections = [
        {
            "id": row.pk,
            "plant_name": row.display_plant_name,
            "company": row.binding.region.tenant.name,
            "status": row.status,
            "kind": _("Forecast")
            if row.endpoint == CollectionEndpoint.FORECASTS
            else _("Past estimates"),
            "target_date": row.target_date,
            "status_label": row.status.replace("_", " ").capitalize(),
            "scheduled_for": row.scheduled_for,
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
            "company": row.schedule.region.tenant.name,
            "recipient": ", ".join(row.recipients),
            "status": row.status,
            "status_label": {
                "sent": _("Accepted by email service"),
                "uncertain": _("Acceptance unknown"),
            }.get(row.status, row.status.replace("_", " ").capitalize()),
            "scheduled_for": row.scheduled_for,
            "sent_at": row.sent_at,
            "last_error": row.last_error,
            "error_label": row.last_error.replace("_", " ").capitalize(),
            "attempts": row.attempts,
            "review_url": reverse("admin:workspace_plant", args=(row.schedule.region_id,)),
            "forecast_status": (
                _("Forecast unavailable")
                if (row.prepared_message or {}).get("kind") == PreparedMessageKind.UNAVAILABLE
                else _("Partial forecast")
                if (row.prepared_message or {}).get("tomorrow_complete") is False
                else ""
            ),
        }
        for row in delivery_rows.select_related("schedule__region__tenant").order_by(
            "-scheduled_for", "-pk"
        )[:limit]
    ]
    metering = [
        {
            "id": row.pk,
            "plant_name": row.plant.name,
            "company": row.plant.region.tenant.name,
            "source_label": (
                _("Reported production")
                if row.source_kind == "realtime_generation"
                else _("Settlement production")
            ),
            "target_date": row.target_date,
            "period_end_day": row.period_end_day,
            "scheduled_for": row.scheduled_for,
            "available_at": row.available_at,
            "status": row.status,
            "status_label": (
                _("Waiting for provider publication")
                if row.status == EpiasJobStatus.AWAITING_PUBLICATION
                else row.status.replace("_", " ").capitalize()
            ),
            "error_label": (
                ""
                if row.status == EpiasJobStatus.AWAITING_PUBLICATION
                and row.last_error == EpiasJobError.UNPUBLISHED_DATA
                else row.last_error.replace("_", " ").capitalize()
            ),
            "review_url": reverse("admin:workspace_plant", args=(row.plant.region_id,))
            + "#epias-title",
        }
        for row in metering_rows.select_related("plant__region__tenant").order_by(
            "-scheduled_for", "-pk"
        )[:limit]
    ]
    return {"collections": collections, "deliveries": deliveries, "metering": metering}


def operations_context(issue: str = "") -> dict[str, Any]:
    now = timezone.now()
    health = delivery_health(now, timedelta(minutes=5))
    summary = summarize_health(health)
    selected = next((check for check in summary.checks if check.key == issue), None)
    issue = selected.key if selected else ""
    section = selected.action_url.rsplit("#", 1)[-1] if selected else ""
    phases = {row.phase: row for row in DispatcherStatus.objects.all()}
    unhealthy_phases = cast(list[str], health["unhealthy_dispatch_phases"])
    return {
        "health": health,
        "health_summary": summary,
        "health_checks": summary.checks,
        "health_filter": selected,
        "show_collections": not issue or section == "collections-title",
        "show_deliveries": not issue or section == "deliveries-title",
        "show_metering": not issue or section == "metering-title",
        "show_dispatch": not issue or section == "dispatch-title",
        "dispatch_phases": [
            {
                "label": label,
                "attempted_at": phases[name].attempted_at if name in phases else None,
                "succeeded_at": phases[name].succeeded_at if name in phases else None,
                "last_error": phases[name].last_error if name in phases else "",
                "unhealthy": name in unhealthy_phases,
                "state_label": _("Needs attention") if name in unhealthy_phases else _("Current"),
            }
            for name, label in DispatcherPhase.choices
        ],
        **recent_work_context(issue=issue, now=now),
    }


class WorkspaceSettingsForm(forms.Form):
    max_recipients = forms.IntegerField(
        min_value=1, max_value=32767, label=gettext_lazy("Recipients per plant")
    )
    expected_max_recipients = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    def apply(self, actor: User) -> None:
        require_platform_admin(actor)
        with transaction.atomic():
            settings, _created = NotificationSettings.objects.select_for_update().get_or_create(
                pk=1
            )
            if settings.max_recipients != self.cleaned_data["expected_max_recipients"]:
                raise ValidationError(_("Settings changed. Reload before saving."))
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
    expected_status = forms.ChoiceField(choices=RegionStatus.choices, widget=forms.HiddenInput)
    location_change_id = forms.IntegerField(min_value=1, required=False, widget=forms.HiddenInput)
    action = forms.ChoiceField(
        choices=tuple(
            (name, name) for name in ("reject", "suspend", "approve_location", "reject_location")
        ),
        widget=forms.HiddenInput,
    )

    def apply(self, actor: User, region_id: int) -> None:
        require_platform_admin(actor)
        data = self.cleaned_data
        with transaction.atomic():
            if data["action"] == "approve_location":
                tenant_id = (
                    Region.objects.active().values_list("tenant_id", flat=True).get(pk=region_id)
                )
                Tenant.objects.select_for_update().get(pk=tenant_id)
            region = Region.objects.active().select_for_update().get(pk=region_id)
            if (
                region.revision != data["expected_revision"]
                or region.status != data["expected_status"]
            ):
                raise ValidationError(_("The plant changed. Reload before reviewing it."))
            if data["action"] in {"reject", "suspend"}:
                required_status = (
                    RegionStatus.PENDING if data["action"] == "reject" else RegionStatus.APPROVED
                )
                if region.status != required_status:
                    raise ValidationError(
                        _("This decision is unavailable for the current review status.")
                    )
                transition_region(
                    actor,
                    region.pk,
                    RegionStatus.REJECTED if data["action"] == "reject" else RegionStatus.SUSPENDED,
                )
            else:
                change = RegionLocationChangeRequest.objects.get(
                    pk=data["location_change_id"],
                    region=region,
                    status=LocationChangeStatus.PENDING,
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
                region=region, status=LocationChangeStatus.PENDING
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
