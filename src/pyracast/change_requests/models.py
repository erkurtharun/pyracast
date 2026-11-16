from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import F, Q


class ChangeRequest(models.Model):
    plant = models.ForeignKey("plants.Plant", on_delete=models.PROTECT)
    configuration = models.ForeignKey("plants.PlantConfiguration", on_delete=models.PROTECT)
    kind = models.CharField(
        max_length=20, choices=(("production", "Production"), ("incident", "Incident"))
    )
    status = models.CharField(
        max_length=20,
        default="pending",
        choices=tuple((v, v) for v in ("pending", "approved", "rejected", "withdrawn")),
    )
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    proposer_membership_id = models.PositiveBigIntegerField()
    idempotency_key = models.UUIDField()
    reason = models.CharField(max_length=500, blank=True)
    payload_sha256 = models.CharField(max_length=64)
    submitted_at = models.DateTimeField(auto_now_add=True)
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
                name="change_request_idempotency_unique",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=("production", "incident")), name="change_request_kind_valid"
            ),
            models.CheckConstraint(
                condition=Q(
                    status="pending",
                    closed_at__isnull=True,
                    reviewed_by__isnull=True,
                    outcome_seen_at__isnull=True,
                    decision_reason="",
                )
                | Q(
                    status="withdrawn",
                    closed_at__isnull=False,
                    reviewed_by__isnull=True,
                    outcome_seen_at__isnull=True,
                )
                | Q(
                    status__in=("approved", "rejected"),
                    closed_at__isnull=False,
                    reviewed_by__isnull=False,
                ),
                name="change_request_review_state",
            ),
        )
        indexes = (models.Index(fields=("plant", "status", "submitted_at")),)


class ProductionChangeItem(models.Model):
    request = models.ForeignKey(
        ChangeRequest, on_delete=models.CASCADE, related_name="production_items"
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    action = models.CharField(max_length=10)
    energy_mwh = models.FloatField()
    quality = models.CharField(max_length=20)
    source = models.CharField(max_length=200)
    expected_head = models.ForeignKey(
        "plants.ProductionObservation", on_delete=models.PROTECT, null=True, related_name="+"
    )
    baseline_energy_mwh = models.FloatField(null=True)
    baseline_source_kind = models.CharField(max_length=40, blank=True)
    applied_observation = models.ForeignKey(
        "plants.ProductionObservation", on_delete=models.PROTECT, null=True, related_name="+"
    )

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=Q(action__in=("set", "revert")), name="proposal_production_action_valid"
            ),
            models.CheckConstraint(
                condition=Q(energy_mwh__gte=0, energy_mwh__lt=float("inf")),
                name="proposal_production_energy_finite",
            ),
            models.CheckConstraint(
                condition=Q(baseline_energy_mwh__isnull=True)
                | Q(baseline_energy_mwh__gte=0, baseline_energy_mwh__lt=float("inf")),
                name="proposal_baseline_energy_finite",
            ),
            models.CheckConstraint(
                condition=Q(ends_at__gt=F("starts_at")), name="proposal_production_interval_valid"
            ),
            models.CheckConstraint(
                condition=Q(quality__in=("valid", "outage", "curtailed", "invalid")),
                name="proposal_production_quality_valid",
            ),
            models.UniqueConstraint(
                fields=("request", "starts_at", "ends_at"),
                name="proposal_production_interval_unique",
            ),
        )


class IncidentChange(models.Model):
    request = models.OneToOneField(
        ChangeRequest, on_delete=models.CASCADE, related_name="incident_change"
    )
    action = models.CharField(max_length=10)
    incident = models.ForeignKey("incidents.PlantIncident", on_delete=models.PROTECT, null=True)
    expected_revision = models.PositiveIntegerField(null=True)
    kind = models.CharField(max_length=20, blank=True)
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
                condition=Q(action="create", incident__isnull=True, expected_revision__isnull=True)
                | Q(
                    action__in=("update", "resolve", "cancel"),
                    incident__isnull=False,
                    expected_revision__gte=1,
                    expected_revision__isnull=False,
                ),
                name="proposal_incident_target_valid",
            ),
            models.CheckConstraint(
                condition=Q(action__in=("resolve", "cancel"))
                | Q(
                    action__in=("create", "update"),
                    starts_at__isnull=False,
                    kind__in=("inverter", "panels", "grid", "other", "multiple"),
                )
                & ~Q(title=""),
                name="proposal_incident_values_valid",
            ),
            models.CheckConstraint(
                condition=~Q(action="resolve") | Q(ends_at__isnull=False),
                name="proposal_incident_end_required",
            ),
            *(
                models.CheckConstraint(
                    condition=Q(**{f"{field}__isnull": True})
                    | Q(**{f"{field}__gte": 0, f"{field}__lt": float("inf")}),
                    name=f"proposal_incident_{name}_finite",
                )
                for field, name in (
                    ("available_dc_mw", "dc"),
                    ("available_inverter_mw", "inverter"),
                    ("available_export_mw", "export"),
                )
            ),
        )
