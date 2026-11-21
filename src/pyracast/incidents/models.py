from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeBoundary, RangeOperators
from django.db import models
from django.db.models import F, Q

from pyracast.db import choices_check
from pyracast.plants.models import TsTzRange


class IncidentKind(models.TextChoices):
    INVERTER = "inverter", "Inverter"
    PANELS = "panels", "Panels"
    GRID = "grid", "Grid"
    OTHER = "other", "Other"
    MULTIPLE = "multiple", "Multiple"


class IncidentStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    RESOLVED = "resolved", "Resolved"
    CANCELLED = "cancelled", "Cancelled"


class PlantIncident(models.Model):
    plant = models.ForeignKey("plants.Plant", on_delete=models.PROTECT)
    declared_configuration = models.ForeignKey(
        "plants.PlantConfiguration", on_delete=models.PROTECT
    )
    kind = models.CharField(max_length=20, choices=IncidentKind)
    title = models.CharField(max_length=200)
    notes = models.CharField(max_length=2000, blank=True)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True)
    available_dc_mw = models.FloatField(null=True)
    available_inverter_mw = models.FloatField(null=True)
    available_export_mw = models.FloatField(null=True)
    cancelled_at = models.DateTimeField(null=True)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = (
            ExclusionConstraint(
                name="%(class)s_plant_interval_excl",
                expressions=(
                    ("plant", RangeOperators.EQUAL),
                    (TsTzRange("starts_at", "ends_at", RangeBoundary()), RangeOperators.OVERLAPS),
                ),
                condition=Q(cancelled_at__isnull=True),
            ),
            models.CheckConstraint(
                condition=Q(ends_at__isnull=True) | Q(ends_at__gt=F("starts_at")),
                name="%(class)s_ends_at_chk",
            ),
            choices_check("kind", IncidentKind),
            models.CheckConstraint(
                condition=Q(available_dc_mw__isnull=True)
                | Q(available_dc_mw__gte=0, available_dc_mw__lt=float("inf")),
                name="%(class)s_available_dc_mw_chk",
            ),
            models.CheckConstraint(
                condition=Q(available_inverter_mw__isnull=True)
                | Q(available_inverter_mw__gte=0, available_inverter_mw__lt=float("inf")),
                name="%(class)s_available_inverter_mw_chk",
            ),
            models.CheckConstraint(
                condition=Q(available_export_mw__isnull=True)
                | Q(available_export_mw__gte=0, available_export_mw__lt=float("inf")),
                name="%(class)s_available_export_mw_chk",
            ),
        )


class IncidentRevision(models.Model):
    incident = models.ForeignKey(PlantIncident, on_delete=models.PROTECT, related_name="revisions")
    revision = models.PositiveIntegerField()
    snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("incident", "revision"), name="%(class)s_incident_revision_uniq"
            ),
        )
