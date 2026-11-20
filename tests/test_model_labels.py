from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from django.core.exceptions import FieldDoesNotExist
from django.db import models

from pyracast.accounts.models import Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    DispatcherStatus,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationSettings,
    ReminderSchedule,
)
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
    ProductionObservation,
)
from pyracast.platform_admin.site import site
from pyracast.regions.models import AuditEvent, Region, RegionLocationChangeRequest

START = datetime(2026, 11, 9, 10, tzinfo=UTC)


def displayed_models() -> Iterator[type[models.Model]]:
    for model, model_admin in site._registry.items():
        yield model
        for name in (*model_admin.list_display, *(model_admin.fields or ())):
            try:
                field = model._meta.get_field(str(name))
            except FieldDoesNotExist:
                continue
            if isinstance(field.related_model, type):
                yield field.related_model


def test_every_model_the_console_shows_has_its_own_label() -> None:
    unlabelled = {
        model._meta.label
        for model in displayed_models()
        if model.__module__.startswith("pyracast.") and model.__str__ is models.Model.__str__
    }
    assert unlabelled == set()


@pytest.mark.parametrize(
    ("row", "label"),
    [
        (Tenant(name="Acme Energy"), "Acme Energy"),
        (Region(name="Bursa field"), "Bursa field"),
        (Plant(name="Bursa plant"), "Bursa plant"),
        (PlantConfiguration(plant_id=4, revision=2), "Plant 4 revision 2"),
        (SiteBinding(site_id="ab12-cd34", region_id=3), "Site ab12-cd34 of region 3"),
        (
            CollectionJob(endpoint="forecasts", target_date=date(2026, 11, 9), binding_id=5),
            "forecasts 2026-11-09 of binding 5",
        ),
        (
            ForecastSnapshot(pk=8, source_kind="estimated_actuals", binding_id=5),
            "estimated_actuals snapshot 8 of binding 5",
        ),
        (ForecastResult(snapshot_id=8), "Result of snapshot 8"),
        (
            RequestAttempt(pk=6, purpose="validation", outcome="failed"),
            "validation request 6: failed",
        ),
        (AuditEvent(action="region_approved", revision=3), "region_approved at revision 3"),
        (
            ProductionObservation(plant_id=4, starts_at=START),
            "Plant 4 from 2026-11-09T10:00:00+00:00",
        ),
        (CalibrationRun(pk=9, plant_id=4, status="ready"), "Calibration 9 of plant 4: ready"),
        (CalibrationActivation(pk=2, plant_id=4), "Activation 2 of plant 4"),
        (PlantEvent(action="plant_renamed", plant_id=4), "plant_renamed on plant 4"),
        (
            NotificationDelivery(pk=12, schedule_id=7, status="sent"),
            "Delivery 12 of schedule 7: sent",
        ),
        (ReminderSchedule(region_id=3), "Reminders of region 3"),
        (
            RegionLocationChangeRequest(pk=1, region_id=3, status="pending"),
            "Location change 1 of region 3: pending",
        ),
        (DispatcherStatus(phase="collection"), "collection"),
        (NotificationSettings(), "Email reminder settings"),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, models.Model) else None,
)
def test_console_labels_name_the_row(row: models.Model, label: str) -> None:
    assert str(row) == label
