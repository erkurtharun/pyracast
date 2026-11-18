from collections.abc import Callable, Iterator
from datetime import timedelta

import pytest
from django.db import models

from pyracast.change_requests.models import IncidentChange, ProductionChangeItem
from pyracast.forecasts.models import CollectionJob, ForecastSnapshot, RequestAttempt
from pyracast.metering.models import EpiasCollectionJob
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.plants.models import CalibrationRun, PlantConfiguration, PlantEvent
from tests.support.change_request_boundaries import (
    RequestCase,
    incident_request,
    make_request_case,
    production_request,
)
from tests.support.domain import NOW, System, make_system
from tests.support.epias_api import archive_source
from tests.support.incident_calibration import incident
from tests.support.metering import connected
from tests.support.migrations import checks, forward, leaf_nodes, migrate
from tests.support.plant_operations import make_plant, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)

SLOT = NOW - timedelta(days=30)

INITIAL = {
    "forecasts": ("forecasts", "0001_initial"),
    "plants": ("plants", "0001_initial"),
    "incidents": ("incidents", "0001_initial"),
    "metering": ("metering", "0001_initial"),
    "notifications": ("notifications", "0001_initial"),
    "change_requests": ("change_requests", "0001_initial"),
}
NEW_CHECKS = {
    ("forecasts_collectionjob", "endpoint"): "collectionjob_endpoint_chk",
    ("forecasts_requestattempt", "purpose"): "requestattempt_purpose_chk",
    ("forecasts_collectionjob", "last_error"): "collectionjob_last_error_chk",
    ("forecasts_forecastsnapshot", "weather_error"): "forecastsnapshot_weather_error_chk",
    ("metering_epiascollectionjob", "last_error"): "epiascollectionjob_last_error_chk",
    ("plants_plantevent", "action"): "plantevent_action_chk",
    ("plants_calibrationrun", "last_error"): "calibrationrun_last_error_chk",
    (
        "change_requests_productionchangeitem",
        "baseline_source_kind",
    ): "productionchangeitem_baseline_source_kind_chk",
    ("change_requests_incidentchange", "kind"): "incidentchange_kind_chk",
    ("notifications_notificationdelivery", "last_error"): "notificationdelivery_last_error_chk",
    (
        "notifications_notificationdelivery",
        "prepared_message__format",
    ): "notificationdelivery_prepared_format_chk",
    (
        "notifications_notificationdelivery",
        "prepared_message__kind",
    ): "notificationdelivery_prepared_kind_chk",
}
UNAVAILABLE_MESSAGE = {
    "format": "plain",
    "kind": "unavailable",
    "subject": "Pyracast forecast unavailable",
    "body": "Tomorrow's saved forecast could not be prepared.",
    "prepared_at": NOW.isoformat(),
}


@pytest.fixture
def leaf() -> Iterator[None]:
    targets = leaf_nodes()
    try:
        yield
    finally:
        migrate(*targets)


def _installed() -> set[str]:
    return {name for (table, _), name in NEW_CHECKS.items() if name in checks(table)}


def _job_and_attempt(system: System) -> tuple[CollectionJob, RequestAttempt]:
    job = CollectionJob.objects.create(
        binding=system.binding, scheduled_slot=SLOT, target_date=SLOT.date(), available_at=SLOT
    )
    attempt = RequestAttempt.objects.create(
        provider_account=system.account,
        binding=system.binding,
        utc_day=NOW.date(),
        created_at=NOW,
        purpose="validation",
    )
    return job, attempt


def _job(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return _job_and_attempt(make_system())[0]


def _attempt(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return _job_and_attempt(make_system())[1]


def _snapshot(monkeypatch: pytest.MonkeyPatch) -> ForecastSnapshot:
    return snapshot_for(make_system(), NOW.date())


def _epias_job(monkeypatch: pytest.MonkeyPatch) -> EpiasCollectionJob:
    return archive_source(connected(monkeypatch)[2], "realtime_generation", {0: "1"}).job


def _plant_event(monkeypatch: pytest.MonkeyPatch) -> PlantEvent:
    return PlantEvent.objects.filter(plant=make_plant(monkeypatch)[1]).earliest("pk")


def _calibration_run(monkeypatch: pytest.MonkeyPatch) -> CalibrationRun:
    system, plant = make_plant(monkeypatch)
    return CalibrationRun.objects.create(
        plant=plant,
        configuration=PlantConfiguration.objects.get(plant=plant),
        binding=system.binding,
        requested_by=system.member,
        window_start=NOW.date() - timedelta(days=7),
        window_end=NOW.date() - timedelta(days=1),
        cutoff_at=NOW,
        status="failed",
        completed_at=NOW,
        last_error="attempt_limit",
    )


def _production_item(monkeypatch: pytest.MonkeyPatch) -> ProductionChangeItem:
    return ProductionChangeItem.objects.get(
        request=production_request(make_request_case(monkeypatch))
    )


def _cancelled_incident_change(case: RequestCase) -> IncidentChange:
    request = incident_request(case)
    target = incident(case.system, case.plant, NOW - timedelta(hours=2), None)
    IncidentChange.objects.filter(request=request).update(
        action="cancel", incident=target, expected_revision=1, kind="", title=""
    )
    return IncidentChange.objects.get(request=request)


def _incident_change(monkeypatch: pytest.MonkeyPatch) -> IncidentChange:
    return _cancelled_incident_change(make_request_case(monkeypatch))


def _failed_delivery(system: System) -> NotificationDelivery:
    return NotificationDelivery.objects.create(
        schedule=ReminderSchedule.objects.create(region=system.region, effective_from=SLOT),
        scheduled_for=SLOT,
        available_at=SLOT,
        binding=system.binding,
        preparation_deadline=SLOT,
        status="failed",
        last_error="mail_rejected",
        prepared_message=UNAVAILABLE_MESSAGE,
    )


def _delivery(monkeypatch: pytest.MonkeyPatch) -> NotificationDelivery:
    return _failed_delivery(make_system())


@pytest.mark.usefixtures("leaf")
def test_vocabulary_migrations_keep_conforming_rows_and_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_request_case(monkeypatch)
    job, attempt = _job_and_attempt(case.system)
    event = PlantEvent.objects.filter(plant=case.plant).earliest("pk")
    item = ProductionChangeItem.objects.get(request=production_request(case))
    created = IncidentChange.objects.get(request=incident_request(case))
    cancelled = _cancelled_incident_change(case)
    delivery = _failed_delivery(case.system)
    migrate(*INITIAL.values())
    assert not _installed()

    forward()

    assert _installed() == set(NEW_CHECKS.values())
    assert CollectionJob.objects.get(pk=job.pk).endpoint == "forecasts"
    assert RequestAttempt.objects.get(pk=attempt.pk).purpose == "validation"
    assert PlantEvent.objects.get(pk=event.pk).action == event.action
    assert ProductionChangeItem.objects.get(pk=item.pk).baseline_source_kind == (
        item.baseline_source_kind
    )
    assert IncidentChange.objects.get(pk=created.pk).kind == "inverter"
    assert IncidentChange.objects.get(pk=cancelled.pk).kind == ""
    delivery.refresh_from_db()
    assert (delivery.last_error, delivery.prepared_message) == (
        "mail_rejected",
        UNAVAILABLE_MESSAGE,
    )
    migrate(*INITIAL.values())
    assert not _installed()


@pytest.mark.usefixtures("leaf")
@pytest.mark.parametrize(
    ("row", "field", "value", "target"),
    [
        (_job, "endpoint", "legacy_endpoint", INITIAL["forecasts"]),
        (_attempt, "purpose", "operator", INITIAL["forecasts"]),
        (_job, "last_error", "provider_timeout", INITIAL["forecasts"]),
        (_snapshot, "weather_error", "weather_timeout", INITIAL["forecasts"]),
        (_epias_job, "last_error", "upstream_error", INITIAL["metering"]),
        (_plant_event, "action", "legacy_action", INITIAL["plants"]),
        (_calibration_run, "last_error", "ValueError", INITIAL["plants"]),
        (_production_item, "baseline_source_kind", "provider", INITIAL["change_requests"]),
        (_incident_change, "kind", "legacy_kind", INITIAL["change_requests"]),
        (_delivery, "last_error", "smtp_failure", INITIAL["notifications"]),
    ],
)
def test_vocabulary_migration_refuses_stored_values_outside_the_vocabulary(
    row: Callable[[pytest.MonkeyPatch], models.Model],
    field: str,
    value: str,
    target: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = row(monkeypatch)
    model, allowed = type(stored), getattr(stored, field)
    table = model._meta.db_table
    migrate(target)
    model._default_manager.filter(pk=stored.pk).update(**{field: value})

    with pytest.raises(ValueError, match=rf"{table}\.{field} holds values outside") as refused:
        forward()

    assert repr(value) in str(refused.value)
    assert NEW_CHECKS[table, field] not in checks(table)
    assert model._default_manager.filter(pk=stored.pk, **{field: value}).exists()
    model._default_manager.filter(pk=stored.pk).update(**{field: allowed})
    forward()
    assert NEW_CHECKS[table, field] in checks(table)


@pytest.mark.usefixtures("leaf")
@pytest.mark.parametrize(("key", "value"), [("format", "html"), ("kind", "legacy_kind")])
def test_vocabulary_migration_refuses_prepared_messages_outside_the_vocabulary(
    key: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivery = _delivery(monkeypatch)
    table, lookup = NotificationDelivery._meta.db_table, f"prepared_message__{key}"
    migrate(INITIAL["notifications"])
    stored = NotificationDelivery.objects.filter(pk=delivery.pk)
    stored.update(prepared_message={**UNAVAILABLE_MESSAGE, key: value})

    with pytest.raises(ValueError, match=rf"{table}\.{lookup} holds values outside") as refused:
        forward()

    assert repr(value) in str(refused.value)
    assert NEW_CHECKS[table, lookup] not in checks(table)
    assert stored.get().prepared_message == {**UNAVAILABLE_MESSAGE, key: value}
    stored.update(prepared_message=UNAVAILABLE_MESSAGE)
    forward()
    assert NEW_CHECKS[table, lookup] in checks(table)
