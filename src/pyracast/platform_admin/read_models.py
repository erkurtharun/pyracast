from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypedDict

from django.core.paginator import Paginator
from django.db.models import Count, OuterRef, Q, QuerySet, Subquery, TextChoices
from django.db.models.fields.json import KT
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJobError,
    CollectionJobStatus,
    ForecastSnapshot,
    SnapshotSourceKind,
)
from pyracast.metering.models import EpiasJobError, EpiasJobStatus
from pyracast.notifications.models import (
    NotificationDeliveryError,
    NotificationDeliveryStatus,
    PreparedMessageKind,
)
from pyracast.notifications.operations import recipient_limit
from pyracast.notifications.recipients import delivery_addresses
from pyracast.orchestration.delivery_health import (
    DISPATCH_LAG,
    TABLES,
    BindingRequests,
    Check,
    Scope,
    Window,
    check,
    delivery_health,
    listed,
    read_broker,
)
from pyracast.plants.models import CalibrationError, CalibrationStatus, Plant
from pyracast.platform_admin.forms import ReviewDecisionForm, WorkspaceSettingsForm
from pyracast.platform_admin.health import PRESENTATION, presented, summarize_health
from pyracast.platform_admin.records import region_records
from pyracast.regions.models import (
    AuditAction,
    AuditEvent,
    LocationChangeStatus,
    Region,
    RegionLocationChangeRequest,
    RegionStatus,
    active_region,
)
from pyracast.regions.review import REVIEW_DECISIONS

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise, WithAnnotations


@dataclass(frozen=True)
class PlantSummary:
    id: int
    name: str
    company: str
    plant_name: str
    status: str
    status_label: StrOrPromise
    revision: int
    created_at: datetime
    latitude: float
    longitude: float
    timezone: str
    connection: str
    last_collected_at: datetime | None
    review_url: str
    issue_reason: str = ""
    issue_time: datetime | None = None


PLANT_SCOPES: frozenset[Scope] = frozenset({"plants"})
OPERATION_SCOPES: frozenset[Scope] = frozenset(
    {"collections", "deliveries", "metering", "calibrations"}
)
LISTED_BECAUSE = {
    "invalid_reminder_recipients": gettext_lazy(
        "A saved reminder recipient is not a valid address."
    ),
    "unvalidated_provider_credentials": gettext_lazy(
        "The provider key changed and the connection is not validated."
    ),
    "blocked_provider_credentials": gettext_lazy("The provider rejected the current key."),
    "provider_cooldowns": gettext_lazy("Provider delay until"),
}


def error_label(vocabulary: type[TextChoices], code: str) -> StrOrPromise:
    return vocabulary(code).label if code else ""


def own_check(issue: str, scopes: frozenset[Scope]) -> Check | None:
    """The console check a page filters by; a page ignores the other pages' keys."""
    if issue not in PRESENTATION or (selected := check(issue)).scope not in scopes:
        return None
    return selected


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


def listed_because(
    issue: str, region: WithAnnotations[Region, WorkspaceAnnotations]
) -> tuple[str, datetime | None]:
    if issue == "regions_without_recent_forecasts":
        if region.workspace_last_collected_at is None:
            return _("No forecast saved yet"), None
        return _("Last forecast"), region.workspace_last_collected_at
    if issue == "provider_cooldowns":
        return str(LISTED_BECAUSE[issue]), region.workspace_request_blocked_until
    return str(LISTED_BECAUSE[issue]), None


def summarize_plant(
    region: WithAnnotations[Region, WorkspaceAnnotations], issue: str = ""
) -> PlantSummary:
    issue_reason, issue_time = listed_because(issue, region) if issue else ("", None)
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
        status_label=RegionStatus(region.status).label,
        revision=region.revision,
        created_at=region.created_at,
        latitude=region.latitude,
        longitude=region.longitude,
        timezone=region.timezone,
        connection=connection,
        last_collected_at=region.workspace_last_collected_at,
        review_url=reverse("admin:workspace_plant", args=(region.pk,)),
        issue_reason=issue_reason,
        issue_time=issue_time,
    )


def plant_list_context(
    query: str, status: str, page: str, issue: str = "", *, location_change: str = ""
) -> dict[str, Any]:
    regions = workspace_regions()
    if location_change == LocationChangeStatus.PENDING:
        regions = regions.filter(
            regionlocationchangerequest__status=LocationChangeStatus.PENDING
        ).distinct()
    else:
        location_change = ""
    selected = own_check(issue, PLANT_SCOPES)
    if selected is not None:
        window = Window(timezone.now(), DISPATCH_LAG)
        regions = regions.filter(pk__in=listed(selected, window).values("pk"))
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
    issue = selected.key if selected else ""
    return {
        "plants": [summarize_plant(region, issue) for region in page_obj],
        "page_obj": page_obj,
        "q": query,
        "status": status,
        "status_choices": RegionStatus.choices,
        "health_filter": presented(issue, page_obj.paginator.count) if selected else None,
        "location_change": location_change,
    }


def overview_context() -> dict[str, Any]:
    regions = workspace_regions()
    health = delivery_health(timezone.now())
    counts = dict.fromkeys(RegionStatus.values, 0)
    counts.update(
        {
            row["status"]: row["count"]
            for row in regions.values("status").annotate(count=Count("pk")).order_by()
        }
    )
    counts["needs_attention"] = sum(
        summarize_plant(region).connection == "Needs attention" for region in regions
    )
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
    """Events of live plants and, on the overview, of provider accounts."""
    events = AuditEvent.objects.filter(
        Q(active_region("region__"), region__isnull=False) | Q(provider_account__isnull=False)
    )
    if region_id is not None:
        events = events.filter(region_id=region_id)
    return [
        {**row, "action_label": str(AuditAction(row["action"]).label)}
        for row in events.order_by("-created_at", "-pk").values(
            "action",
            "created_at",
            "region__name",
            "provider_account__external_account_id",
            "actor__username",
        )[:limit]
    ]


def recent_work_context(
    region_id: int | None = None,
    limit: int = 20,
    *,
    issue: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    window = Window(now or timezone.now(), DISPATCH_LAG)
    selected = own_check(issue, OPERATION_SCOPES)

    def rows(scope: Scope) -> QuerySet[Any]:
        if selected is not None and selected.scope == scope:
            return listed(selected, window)
        return TABLES[scope]()

    collection_rows = rows("collections").annotate(
        display_plant_name=Coalesce(
            Subquery(
                Plant.objects.filter(region_id=OuterRef("binding__region_id"))
                .order_by("pk")
                .values("name")[:1]
            ),
            "binding__region__name",
        )
    )
    delivery_rows = rows("deliveries").annotate(
        display_plant_name=Coalesce(
            Subquery(
                Plant.objects.filter(region_id=OuterRef("schedule__region_id"))
                .order_by("pk")
                .values("name")[:1]
            ),
            "schedule__region__name",
        )
    )
    metering_rows = rows("metering")
    calibration_rows = rows("calibrations")
    if region_id is not None:
        collection_rows = collection_rows.filter(binding__region_id=region_id)
        delivery_rows = delivery_rows.filter(schedule__region_id=region_id)
        metering_rows = metering_rows.filter(plant__region_id=region_id)
        calibration_rows = calibration_rows.filter(plant__region_id=region_id)
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
            "status_label": CollectionJobStatus(row.status).label,
            "scheduled_for": row.scheduled_for,
            "last_error": row.last_error,
            "error_label": error_label(CollectionJobError, row.last_error),
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
            "recipient": ", ".join(delivery_addresses(row)),
            "status": row.status,
            "status_label": {
                "sent": _("Accepted by email service"),
                "uncertain": _("Acceptance unknown"),
            }.get(row.status, NotificationDeliveryStatus(row.status).label),
            "scheduled_for": row.scheduled_for,
            "sent_at": row.sent_at,
            "last_error": row.last_error,
            "error_label": error_label(NotificationDeliveryError, row.last_error),
            "attempts": row.attempts,
            "review_url": reverse("admin:workspace_plant", args=(row.schedule.region_id,)),
            "forecast_status": (
                _("Forecast unavailable")
                if row.forecast_kind == PreparedMessageKind.UNAVAILABLE
                else _("Partial forecast")
                if row.forecast_complete is False
                else ""
            ),
        }
        for row in delivery_rows.select_related("schedule__region__tenant")
        .prefetch_related("recipient_rows")
        .order_by("-scheduled_for", "-pk")[:limit]
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
                else EpiasJobStatus(row.status).label
            ),
            "error_label": (
                ""
                if row.status == EpiasJobStatus.AWAITING_PUBLICATION
                and row.last_error == EpiasJobError.UNPUBLISHED_DATA
                else error_label(EpiasJobError, row.last_error)
            ),
            "review_url": reverse("admin:workspace_plant", args=(row.plant.region_id,))
            + "#epias-title",
        }
        for row in metering_rows.select_related("plant__region__tenant").order_by(
            "-scheduled_for", "-pk"
        )[:limit]
    ]
    calibrations = [
        {
            "id": run.pk,
            "plant_name": run.plant.name,
            "company": run.plant.region.tenant.name,
            "window_start": run.window_start,
            "window_end": run.window_end,
            "created_at": run.created_at,
            "status": run.status,
            "status_label": CalibrationStatus(run.status).label,
            "attempts": run.attempts,
            "error_label": error_label(CalibrationError, run.last_error),
            "review_url": reverse("admin:workspace_plant", args=(run.plant.region_id,)),
        }
        for run in calibration_rows.select_related("plant__region__tenant").order_by(
            "-created_at", "-pk"
        )[:limit]
    ]
    return {
        "collections": collections,
        "deliveries": deliveries,
        "metering": metering,
        "calibrations": calibrations,
    }


def provider_request_rows(requests: tuple[BindingRequests, ...]) -> list[dict[str, Any]]:
    names = dict(
        SiteBinding.objects.filter(pk__in=[item.binding_id for item in requests])
        .annotate(
            display_plant_name=Coalesce(
                Subquery(
                    Plant.objects.filter(region_id=OuterRef("region_id"))
                    .order_by("pk")
                    .values("name")[:1]
                ),
                "region__name",
            )
        )
        .values_list("pk", "display_plant_name")
    )
    return [
        {"plant_name": names[item.binding_id], "today": item.today, "busiest_day": item.busiest_day}
        for item in requests
    ]


def operations_context(issue: str = "") -> dict[str, Any]:
    now = timezone.now()
    health = delivery_health(now, broker=read_broker())
    summary = summarize_health(health)
    selected = own_check(issue, OPERATION_SCOPES)
    dispatch = (
        next((item for item in summary.checks if item.key == "dispatch"), None)
        if issue == "dispatch"
        else None
    )
    section = selected.scope if selected else "dispatch" if dispatch else ""
    return {
        "health": health,
        "health_summary": summary,
        "health_filter": presented(issue, health.counts[issue]) if selected else dispatch,
        "show_collections": not section or section == "collections",
        "show_deliveries": not section or section == "deliveries",
        "show_metering": not section or section == "metering",
        "show_calibrations": not section or section == "calibrations",
        "show_dispatch": not section or section == "dispatch",
        "listed_total": health.counts[issue] if selected else None,
        "dispatch_phases": [
            {
                "label": state.phase.label,
                "attempted_at": state.attempted_at,
                "succeeded_at": state.succeeded_at,
                "last_error": state.last_error,
                "duration_ms": state.duration_ms,
                "published": state.published,
                "unhealthy": not state.healthy,
                "state_label": _("Current") if state.healthy else _("Needs attention"),
            }
            for state in health.phases
        ],
        "broker": health.broker,
        "provider_requests": provider_request_rows(health.requests[:20]),
        **recent_work_context(issue=issue, now=now),
    }


MEAN_EARTH_RADIUS_KM = 6371.0088


def distance_km(start: tuple[float, float], end: tuple[float, float]) -> float:
    """Great-circle distance between two (latitude, longitude) points in degrees, on a sphere of
    the mean Earth radius: a review aid, not a survey measure."""
    lat1, lon1 = map(math.radians, start)
    lat2, lon2 = map(math.radians, end)
    half = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    # Rounding can lift `half` a unit in the last place above 1 for antipodal points.
    return 2 * MEAN_EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, half)))


def review_history(region_id: int) -> dict[str, Any]:
    """The latest review decision with its reason, and whether the customer resubmitted the
    plant after it."""
    events = list(
        AuditEvent.objects.filter(
            region_id=region_id, action__in=(*REVIEW_DECISIONS, AuditAction.REGION_RESUBMITTED)
        )
        .annotate(reason=KT("details__reason"))
        .order_by("-created_at", "-pk")
        .values("action", "reason", "created_at")[:2]
    )
    resubmitted = bool(events) and events[0]["action"] == AuditAction.REGION_RESUBMITTED
    decision = events[1 if resubmitted else 0] if len(events) > resubmitted else None
    return {
        "decision": decision and {**decision, "label": AuditAction(decision["action"]).label},
        "resubmitted_at": events[0]["created_at"] if resubmitted else None,
    }


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
        "region_records": region_records(region.pk),
        "review_history": review_history(region.pk),
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
                "distance_km": distance_km(
                    (region.latitude, region.longitude), (change.latitude, change.longitude)
                ),
                "timezone_changes": change.timezone != region.timezone,
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
