from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import F, Q

from pyracast.db import choices_check
from pyracast.incidents.models import IncidentKind
from pyracast.plants.models import ObservationQuality


class ChangeRequestKind(models.TextChoices):
    PRODUCTION = "production", "Production"
    INCIDENT = "incident", "Incident"


class ChangeRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    WITHDRAWN = "withdrawn", "Withdrawn"


class ProductionChangeAction(models.TextChoices):
    SET = "set", "Set"
    REVERT = "revert", "Revert"


class IncidentChangeAction(models.TextChoices):
    CREATE = "create", "Create"
    UPDATE = "update", "Update"
    RESOLVE = "resolve", "Resolve"
    CANCEL = "cancel", "Cancel"


class BaselineSourceKind(models.TextChoices):
    MANUAL = "manual", "Manual"
    REALTIME_GENERATION = "realtime_generation", "Real-time generation"
    SETTLEMENT_INJECTION = "settlement_injection", "Settlement injection"


DECIDED = (ChangeRequestStatus.APPROVED, ChangeRequestStatus.REJECTED)
INCIDENT_EDITS = (IncidentChangeAction.CREATE, IncidentChangeAction.UPDATE)


class ChangeRequest(models.Model):
    plant = models.ForeignKey("plants.Plant", on_delete=models.PROTECT)
    configuration = models.ForeignKey("plants.PlantConfiguration", on_delete=models.PROTECT)
    kind = models.CharField(max_length=20, choices=ChangeRequestKind)
    status = models.CharField(
        max_length=20, choices=ChangeRequestStatus, default=ChangeRequestStatus.PENDING
    )
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    proposer_membership_id = models.PositiveBigIntegerField()
    idempotency_key = models.UUIDField()
    reason = models.CharField(max_length=500, blank=True)
    payload_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True
    )
    closed_at = models.DateTimeField(null=True)
    decision_reason = models.CharField(max_length=500, blank=True)
    outcome_seen_at = models.DateTimeField(null=True)
    reviewed_context = models.JSONField(default=dict, encoder=DjangoJSONEncoder)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("proposer_membership_id", "idempotency_key"),
                name="%(class)s_idempotency_key_uniq",
            ),
            choices_check("kind", ChangeRequestKind),
            choices_check("status", ChangeRequestStatus),
            models.CheckConstraint(
                condition=Q(
                    status=ChangeRequestStatus.PENDING,
                    closed_at__isnull=True,
                    reviewed_by__isnull=True,
                    outcome_seen_at__isnull=True,
                    decision_reason="",
                )
                | Q(
                    status=ChangeRequestStatus.WITHDRAWN,
                    closed_at__isnull=False,
                    reviewed_by__isnull=True,
                    outcome_seen_at__isnull=True,
                )
                | Q(
                    status__in=DECIDED,
                    closed_at__isnull=False,
                    reviewed_by__isnull=False,
                ),
                name="%(class)s_review_chk",
            ),
            models.CheckConstraint(
                condition=~Q(status=ChangeRequestStatus.REJECTED) | ~Q(decision_reason=""),
                name="%(class)s_decision_reason_chk",
            ),
        )
        indexes = (
            models.Index(
                fields=("plant", "status", "created_at"), name="%(class)s_plant_status_idx"
            ),
        )


class ProductionChangeItem(models.Model):
    request = models.ForeignKey(
        ChangeRequest, on_delete=models.CASCADE, related_name="production_items"
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    action = models.CharField(max_length=10, choices=ProductionChangeAction)
    energy_mwh = models.FloatField()
    quality = models.CharField(max_length=20, choices=ObservationQuality)
    source = models.CharField(max_length=200)
    expected_head = models.ForeignKey(
        "plants.ProductionObservation", on_delete=models.PROTECT, null=True, related_name="+"
    )
    baseline_energy_mwh = models.FloatField(null=True)
    baseline_source_kind = models.CharField(max_length=40, choices=BaselineSourceKind, blank=True)
    applied_observation = models.ForeignKey(
        "plants.ProductionObservation", on_delete=models.PROTECT, null=True, related_name="+"
    )

    class Meta:
        constraints = (
            choices_check("action", ProductionChangeAction),
            models.CheckConstraint(
                condition=Q(energy_mwh__gte=0, energy_mwh__lt=float("inf")),
                name="%(class)s_energy_mwh_chk",
            ),
            models.CheckConstraint(
                condition=Q(baseline_energy_mwh__isnull=True)
                | Q(baseline_energy_mwh__gte=0, baseline_energy_mwh__lt=float("inf")),
                name="%(class)s_baseline_energy_mwh_chk",
            ),
            models.CheckConstraint(
                condition=Q(ends_at__gt=F("starts_at")), name="%(class)s_ends_at_chk"
            ),
            choices_check("quality", ObservationQuality),
            choices_check("baseline_source_kind", BaselineSourceKind, blank=True),
            models.UniqueConstraint(
                fields=("request", "starts_at", "ends_at"),
                name="%(class)s_request_starts_at_ends_at_uniq",
            ),
        )


class IncidentChange(models.Model):
    request = models.OneToOneField(
        ChangeRequest, on_delete=models.CASCADE, related_name="incident_change"
    )
    action = models.CharField(max_length=10, choices=IncidentChangeAction)
    incident = models.ForeignKey("incidents.PlantIncident", on_delete=models.PROTECT, null=True)
    expected_revision = models.PositiveIntegerField(null=True)
    kind = models.CharField(max_length=20, choices=IncidentKind, blank=True)
    title = models.CharField(max_length=200, blank=True)
    notes = models.CharField(max_length=2000, blank=True)
    starts_at = models.DateTimeField(null=True)
    ends_at = models.DateTimeField(null=True)
    available_dc_mw = models.FloatField(null=True)
    available_inverter_mw = models.FloatField(null=True)
    available_export_mw = models.FloatField(null=True)
    applied_revision = models.ForeignKey(
        "incidents.IncidentRevision", on_delete=models.PROTECT, null=True
    )

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=Q(
                    action=IncidentChangeAction.CREATE,
                    incident__isnull=True,
                    expected_revision__isnull=True,
                )
                | Q(
                    action__in=(
                        IncidentChangeAction.UPDATE,
                        IncidentChangeAction.RESOLVE,
                        IncidentChangeAction.CANCEL,
                    ),
                    incident__isnull=False,
                    expected_revision__gte=1,
                    expected_revision__isnull=False,
                ),
                name="%(class)s_target_chk",
            ),
            models.CheckConstraint(
                condition=Q(action__in=(IncidentChangeAction.RESOLVE, IncidentChangeAction.CANCEL))
                | Q(
                    action__in=INCIDENT_EDITS,
                    starts_at__isnull=False,
                    kind__in=IncidentKind.values,
                )
                & ~Q(title=""),
                name="%(class)s_values_chk",
            ),
            models.CheckConstraint(
                condition=~Q(action=IncidentChangeAction.RESOLVE) | Q(ends_at__isnull=False),
                name="%(class)s_resolution_chk",
            ),
            models.CheckConstraint(
                condition=Q(starts_at__isnull=True)
                | Q(ends_at__isnull=True)
                | Q(ends_at__gt=F("starts_at")),
                name="%(class)s_ends_at_chk",
            ),
            choices_check("action", IncidentChangeAction),
            choices_check("kind", IncidentKind, blank=True),
            *(
                models.CheckConstraint(
                    condition=Q(**{f"{field}__isnull": True})
                    | Q(**{f"{field}__gte": 0, f"{field}__lt": float("inf")}),
                    name=f"%(class)s_{field}_chk",
                )
                for field in ("available_dc_mw", "available_inverter_mw", "available_export_mw")
            ),
        )
