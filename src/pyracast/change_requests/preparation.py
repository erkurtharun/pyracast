import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext

from pyracast.change_requests.models import (
    DECIDED,
    INCIDENT_EDITS,
    ChangeRequest,
    ChangeRequestKind,
    IncidentChange,
    IncidentChangeAction,
    ProductionChangeAction,
)
from pyracast.incidents.models import PlantIncident
from pyracast.incidents.operations import prepare_incident, prepare_incident_finish
from pyracast.plants.measurements import select_measurements, select_provider_measurements
from pyracast.plants.models import Plant
from pyracast.plants.production import slot_value
from pyracast.plants.production_changes import (
    PreparedChange,
    ProductionChange,
    lock_production_configuration,
    prepare_production_changes,
    production_change_warnings,
)

INCIDENT_FIELDS = (
    "kind",
    "title",
    "notes",
    "starts_at",
    "ends_at",
    "available_dc_mw",
    "available_inverter_mw",
    "available_export_mw",
)


def digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
        )
    except ValueError as error:
        raise ValidationError(gettext("Request numbers must be finite.")) from error
    return hashlib.sha256(encoded.encode()).hexdigest()


def production_changes(request: ChangeRequest) -> list[ProductionChange]:
    return [
        ProductionChange(
            row.starts_at,
            row.ends_at,
            row.expected_head_id,
            "revert" if row.action == ProductionChangeAction.REVERT else "set",
            row.energy_mwh,
            row.quality,
            row.source,
        )
        for row in request.production_items.order_by("starts_at", "pk")
    ]


def incident_values(change: IncidentChange) -> dict[str, Any]:
    return {field: getattr(change, field) for field in INCIDENT_FIELDS}


def prepare_incident_change(
    actor: User,
    plant: Plant,
    *,
    action: str,
    incident_id: int | None,
    expected_revision: int | None,
    values: dict[str, Any] | None,
    ends_at: datetime | None,
    lock: bool = True,
) -> PlantIncident:
    if action not in IncidentChangeAction.values:
        raise ValidationError(gettext("Unknown incident request action."))
    if (
        action == IncidentChangeAction.CREATE
        and (incident_id is not None or expected_revision is not None)
    ) or (
        action != IncidentChangeAction.CREATE and (incident_id is None or expected_revision is None)
    ):
        raise ValidationError(
            gettext("Incident identity and expected revision are required for existing incidents.")
        )
    if action in INCIDENT_EDITS:
        if values is None or ends_at is not None:
            raise ValidationError(gettext("Incident values are required for create and update."))
        return prepare_incident(
            actor,
            plant,
            incident_id=incident_id,
            expected_revision=expected_revision,
            values=values,
            lock=lock,
        )
    if (
        values is not None
        or incident_id is None
        or expected_revision is None
        or (action == IncidentChangeAction.CANCEL and ends_at is not None)
    ):
        raise ValidationError(gettext("Unsupported incident request fields."))
    return prepare_incident_finish(
        actor,
        plant,
        incident_id=incident_id,
        expected_revision=expected_revision,
        ends_at=ends_at,
        cancel=action == IncidentChangeAction.CANCEL,
        lock=lock,
    )


def validate_request(
    actor: User, request: ChangeRequest, plant: Plant, *, lock: bool = True
) -> None:
    if request.kind == ChangeRequestKind.PRODUCTION:
        configuration = lock_production_configuration(
            plant.region.tenant_id, plant.region_id, plant.pk, request.configuration_id, lock=lock
        )
        prepare_production_changes(configuration, "MWh", production_changes(request))
        return
    change = request.incident_change
    if change.action in INCIDENT_EDITS:
        target = (
            PlantIncident.objects.filter(pk=change.incident_id, plant=plant).first()
            if change.incident_id is not None
            else None
        )
        configuration_id = (
            target.declared_configuration_id
            if target and target.ends_at is not None
            else plant.current_configuration_id
        )
        if configuration_id != request.configuration_id:
            raise ValidationError(gettext("The plant configuration changed. Submit a new request."))
    prepare_incident_change(
        actor,
        plant,
        action=change.action,
        incident_id=change.incident_id,
        expected_revision=change.expected_revision,
        values=incident_values(change) if change.action in INCIDENT_EDITS else None,
        ends_at=change.ends_at if change.action == IncidentChangeAction.RESOLVE else None,
        lock=lock,
    )


def production_context(request: ChangeRequest) -> list[dict[str, Any]]:
    if request.status in DECIDED and request.reviewed_context.get("production_items"):
        items_by_id = {row.pk: row.applied_observation_id for row in request.production_items.all()}
        return [
            {**row, "applied_observation_id": items_by_id[row["id"]]}
            for row in request.reviewed_context["production_items"]
        ]
    items = list(
        request.production_items.select_related("expected_head").order_by("starts_at", "pk")
    )
    start = min(row.starts_at for row in items).replace(minute=0, second=0, microsecond=0)
    last = max(row.ends_at for row in items)
    end = last.replace(minute=0, second=0, microsecond=0)
    if end < last:
        end += timedelta(hours=1)
    effective = select_measurements(request.configuration, start, end, timezone.now())[0]
    providers = select_provider_measurements(request.configuration, start, end, timezone.now())[0]
    warnings = production_change_warnings(
        request.configuration,
        [
            PreparedChange(
                row.starts_at,
                row.ends_at,
                row.energy_mwh,
                row.quality,
                row.source,
                row.action == ProductionChangeAction.REVERT,
                row.expected_head,
            )
            for row in items
        ],
    )
    result = [
        {
            "id": row.pk,
            "starts_at": row.starts_at,
            "ends_at": row.ends_at,
            "action": row.action,
            "expected_revision_id": row.expected_head_id,
            "baseline_energy_mwh": row.baseline_energy_mwh,
            "baseline_source_kind": row.baseline_source_kind,
            "current": slot_value(effective, row.starts_at, row.ends_at),
            "automatic": slot_value(providers, row.starts_at, row.ends_at),
            "provider_intervals": [
                {
                    "starts_at": value.starts_at,
                    "ends_at": value.ends_at,
                    "energy_mwh": value.energy_mwh,
                    "quality": value.quality,
                    "source_kind": value.snapshot.source_kind if value.snapshot else "manual",
                }
                for value in providers
                if value.starts_at < row.ends_at and value.ends_at > row.starts_at
            ],
            "warnings": [
                warning
                for warning in warnings
                if warning["starts_at"] < row.ends_at and warning["ends_at"] > row.starts_at
            ],
            "energy_mwh": row.energy_mwh if row.action == ProductionChangeAction.SET else None,
            "quality": row.quality,
            "source": row.source,
            "applied_observation_id": row.applied_observation_id,
        }
        for row in items
    ]
    for row in result:
        row["proposed"] = (
            {
                "energy_mwh": row["energy_mwh"],
                "quality": row["quality"],
                "source": row["source"],
                "source_kind": "manual",
                "provisional": False,
                "aggregated": False,
            }
            if row["action"] == ProductionChangeAction.SET
            else row["automatic"]
        )
    return result


def review_context(
    request: ChangeRequest, plant: Plant, *, production_items: list[dict[str, Any]]
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "request_id": request.pk,
        "payload": request.payload_sha256,
        "configuration_id": plant.current_configuration_id,
    }
    if request.kind == ChangeRequestKind.PRODUCTION:
        context["items"] = [
            {
                key: row[key]
                for key in (
                    "starts_at",
                    "ends_at",
                    "current",
                    "automatic",
                    "provider_intervals",
                    "warnings",
                )
            }
            for row in production_items
        ]
        overlaps = Q(pk__in=[])
        for item in request.production_items.all():
            overlaps |= Q(starts_at__lt=item.ends_at, ends_at__gt=item.starts_at)
        context["heads"] = list(
            plant.productionobservation_set.filter(overlaps, is_current=True)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
    else:
        change = request.incident_change
        relevant = Q(pk=change.incident_id)
        if change.action in INCIDENT_EDITS:
            overlap = Q(cancelled_at__isnull=True) & (
                Q(ends_at__isnull=True) | Q(ends_at__gt=change.starts_at)
            )
            if change.ends_at is not None:
                overlap &= Q(starts_at__lt=change.ends_at)
            relevant |= overlap
        context["incidents"] = list(
            PlantIncident.objects.filter(relevant, plant=plant)
            .order_by("pk")
            .values_list("pk", "revision")
        )
    return context


def decision_context(request: ChangeRequest, plant: Plant) -> dict[str, Any]:
    items = production_context(request) if request.kind == ChangeRequestKind.PRODUCTION else []
    return {
        "version": 1,
        "context": review_context(request, plant, production_items=items),
        "production_items": items,
    }
