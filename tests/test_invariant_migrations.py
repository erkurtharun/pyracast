import json
import math
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from django.db import connection, models
from django.db.models import F
from psycopg import sql

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    HistoricalReferenceImport,
    ReferenceCapacityNormalization,
)
from pyracast.metering.models import EpiasCollectionJob
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.plants.models import CalibrationRun, Plant, PlantConfiguration, ProductionObservation
from tests.support.capacity_normalization import normalize, scaled_system
from tests.support.domain import NOW, System, make_system
from tests.support.invariants import (
    BINDING_REVISION_RULE,
    CALIBRATION_RUN_RULE,
    IMPORT_SHAPE_RULE,
    LEASE_RULES,
    NORMALIZATION_FACTOR_RULE,
    OBSERVATION_LINEAGE_RULE,
    PLANT_AUTOMATION_RULE,
    PROVIDER_ACCOUNT_RULE,
    SITE_BINDING_RULE,
    STORED_RULES,
    lease_violations,
    stored_violations,
    violations,
)
from tests.support.metering import connected
from tests.support.migrations import (
    checks,
    constraints,
    forward,
    leaf_nodes,
    migrate,
    row_values,
    triggers,
)
from tests.support.plant_operations import make_plant, request_run
from tests.support.plant_training import actual_snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = {
    "connections": ("connections", "0002_initial"),
    "forecasts": ("forecasts", "0002_collection_endpoint_and_request_purpose_checks"),
    "metering": ("metering", "0002_epias_vocabulary_choices"),
    "notifications": ("notifications", "0002_delivery_vocabulary_checks"),
    "plants": ("plants", "0002_plant_event_action_and_calibration_error_checks"),
}
NEW_CHECKS = {
    "connections_provideraccount": {
        "provideraccount_daily_quota_chk",
        "provideraccount_credential_revision_chk",
        "provideraccount_authentication_failed_revision_chk",
    },
    "connections_sitebinding": {"sitebinding_validated_chk"},
    "forecasts_collectionjob": {
        "collectionjob_lease_chk",
        "collectionjob_reference_import_chk",
        "collectionjob_import_status_chk",
    },
    "forecasts_referencecapacitynormalization": {
        "referencecapacitynormalization_target_over_source_chk"
    },
    "metering_epiascollectionjob": {"epiascollectionjob_lease_chk"},
    "plants_calibrationrun": {
        "calibrationrun_lease_chk",
        "calibrationrun_completed_at_chk",
        "calibrationrun_assessment_chk",
    },
    "plants_plant": {"plant_automation_chk"},
    "notifications_notificationdelivery": {"notificationdelivery_lease_chk"},
}
PLANT_TABLES = ("plants_calibrationrun", "plants_plant")
# The audit as the columns before the naming migrations spell it, on the tables that exist then.
PREVIOUS_RULES = tuple(
    (table, rule.replace("scheduled_for", "scheduled_slot"))
    for table, rule in STORED_RULES
    if table != "notifications_outboundmail"
)
NEW_TRIGGERS = {
    "forecastsnapshot_publication_guard",
    "historicalreferenceimport_immutable",
    "epiassnapshot_immutable",
    "productionobservation_revision_guard",
}
CLEAN = dict.fromkeys(
    (
        "forecasts_collectionjob",
        "metering_epiascollectionjob",
        "plants_calibrationrun",
        "notifications_notificationdelivery",
    ),
    0,
)
LEASES_CLEAN = dict.fromkeys(LEASE_RULES, 0)
UNAVAILABLE = {
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


def _installed() -> tuple[set[str], set[str]]:
    found = {name for table in NEW_CHECKS for name in checks(table) if name in NEW_CHECKS[table]}
    unique = {
        name
        for name, details in constraints("connections_sitebinding").items()
        if details["unique"] and name == "sitebinding_region_region_revision_uniq"
    }
    return found | unique, triggers() & NEW_TRIGGERS


def _all_new() -> tuple[set[str], set[str]]:
    return (
        set().union(*NEW_CHECKS.values()) | {"sitebinding_region_region_revision_uniq"},
        NEW_TRIGGERS,
    )


def audit() -> dict[str, int]:
    """The stored audit under the collection slot column of the migrated state."""
    with connection.cursor() as cursor:
        description = connection.introspection.get_table_description(
            cursor, "forecasts_collectionjob"
        )
    named = "scheduled_for" in {column.name for column in description}
    return stored_violations(STORED_RULES if named else PREVIOUS_RULES)


def stored_row(rows: models.QuerySet[Any]) -> dict[str, Any]:
    """The row under the column names of the migrated state."""
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT to_jsonb(stored) FROM {} AS stored WHERE id = %s").format(
                sql.Identifier(rows.model._meta.db_table)
            ),
            [rows.values_list("pk", flat=True).get()],
        )
        (row,) = cursor.fetchone()
    return dict(json.loads(row))


def _tuples() -> dict[tuple[str, int], str]:
    """The physical row version of every job row: an UPDATE gives a row a new xmin."""
    found: dict[tuple[str, int], str] = {}
    with connection.cursor() as cursor:
        for table in CLEAN:
            cursor.execute(sql.SQL("SELECT id, xmin::text FROM {}").format(sql.Identifier(table)))
            found |= {(table, key): version for key, version in cursor.fetchall()}
    return found


@pytest.mark.usefixtures("leaf")
def test_invariant_migrations_repair_the_named_rows_and_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _ = connected(monkeypatch)
    collection = list(CollectionJob.objects.order_by("pk"))
    released = CollectionJob.objects.create(
        binding=system.binding,
        scheduled_for=NOW + timedelta(hours=1),
        target_date=NOW.date(),
        available_at=NOW,
    )
    held = collection[0]
    request_run(system, plant)
    delivery = _delivery(system)
    original = _observation(system, plant, NOW - timedelta(days=2))
    ProductionObservation.objects.filter(pk=original.pk).update(is_current=False)
    correction = _observation(system, plant, NOW - timedelta(days=2), supersedes=original)
    finished = _finished_run(system, plant)
    automated = Plant.objects.filter(pk=plant.pk)
    automated.update(
        automatic_calibration_enabled=True,
        automatic_calibration_enabled_by=system.member,
        next_calibration_check_at=NOW,
    )
    stored_plant = automated.values().get()
    migrate(*PREVIOUS.values())
    assert _installed() == (set(), set())
    token = uuid.uuid4()
    # Main's claim marked an expired running job stale and kept its lease.
    CollectionJob.objects.filter(pk=released.pk).update(
        status="stale", lease_token=token, lease_expires_at=NOW
    )
    CollectionJob.objects.filter(pk=held.pk).update(
        status="running", lease_token=token, lease_expires_at=NOW + timedelta(minutes=5)
    )
    NotificationDelivery.objects.filter(pk=delivery.pk).update(
        status="uncertain", lease_token=token, lease_expires_at=NOW
    )
    valid = _tuples()
    del valid["forecasts_collectionjob", released.pk]
    stored_account = row_values(ProviderAccount.objects.filter(pk=system.account.pk))
    stored_binding = row_values(SiteBinding.objects.filter(pk=system.binding.pk))
    assert set(audit()) == {f"forecasts_collectionjob: {LEASE_RULES['forecasts_collectionjob']}"}

    forward()

    assert stored_violations() == {}
    assert _installed() == _all_new()
    assert lease_violations() == LEASES_CLEAN
    released.refresh_from_db()
    assert (released.status, released.lease_token, released.lease_expires_at) == (
        "stale",
        None,
        None,
    )
    assert {key: _tuples()[key] for key in valid} == valid
    held.refresh_from_db()
    assert (held.lease_token, held.lease_expires_at) == (token, NOW + timedelta(minutes=5))
    delivery.refresh_from_db()
    assert (delivery.status, delivery.lease_token, delivery.lease_expires_at) == (
        "uncertain",
        token,
        NOW,
    )
    assert (
        row_values(ProviderAccount.objects.filter(pk=system.account.pk), stored_account)
        == stored_account
    )
    assert row_values(SiteBinding.objects.filter(pk=system.binding.pk), stored_binding) == (
        stored_binding
    )
    assert violations("connections_provideraccount", PROVIDER_ACCOUNT_RULE) == 0
    assert violations("connections_sitebinding", SITE_BINDING_RULE) == 0
    assert ProductionObservation.objects.get(pk=correction.pk).supersedes_id == original.pk
    assert violations("plants_productionobservation", OBSERVATION_LINEAGE_RULE) == 0
    finished.refresh_from_db()
    assert (finished.status, finished.completed_at, finished.assessment) == (
        "insufficient",
        NOW,
        "insufficient",
    )
    backfilled = automated.values().get()
    assert backfilled["calibration_preparation_due_at"] is not None
    assert {**backfilled, "calibration_preparation_due_at": None} == stored_plant
    assert violations("plants_calibrationrun", CALIBRATION_RUN_RULE) == 0
    assert violations("plants_plant", PLANT_AUTOMATION_RULE) == 0

    migrate(*PREVIOUS.values())
    assert _installed() == (set(), set())


def _finished_run(system: System, plant: Plant, **values: object) -> CalibrationRun:
    return CalibrationRun.objects.create(
        **{
            "plant": plant,
            "configuration_id": plant.current_configuration_id,
            "binding": system.binding,
            "requested_by": system.member,
            "window_start": NOW.date() - timedelta(days=7),
            "window_end": NOW.date() - timedelta(days=1),
            "cutoff_at": NOW,
            "status": "insufficient",
            "assessment": "insufficient",
            "completed_at": NOW,
        }
        | values
    )


def _terminal_run_without_completion(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    system, plant = make_plant(monkeypatch)
    run = _finished_run(system, plant)
    migrate(PREVIOUS["plants"])
    CalibrationRun.objects.filter(pk=run.pk).update(completed_at=None)
    return run


def _assessment_outside(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    system, plant = make_plant(monkeypatch)
    run = _finished_run(system, plant)
    migrate(PREVIOUS["plants"])
    CalibrationRun.objects.filter(pk=run.pk).update(assessment="excellent")
    return run


def _automation_without_user(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    _, plant = make_plant(monkeypatch)
    migrate(PREVIOUS["plants"])
    Plant.objects.filter(pk=plant.pk).update(
        automatic_calibration_enabled=True, next_calibration_check_at=NOW
    )
    return plant


@pytest.mark.usefixtures("leaf")
@pytest.mark.parametrize(
    ("stored", "refusal", "correction"),
    [
        (
            _terminal_run_without_completion,
            r"plants_calibrationrun holds 1 row.*completion time",
            {"completed_at": NOW},
        ),
        (
            _assessment_outside,
            r"plants_calibrationrun\.assessment holds values outside.*'excellent'",
            {"assessment": "evaluated"},
        ),
        (
            _automation_without_user,
            r"plants_plant holds 1 row.*automation",
            {"automatic_calibration_enabled": False, "next_calibration_check_at": None},
        ),
    ],
)
def test_calibration_migration_refuses_run_and_automation_states_no_writer_produces(
    stored: Callable[[pytest.MonkeyPatch], models.Model],
    refusal: str,
    correction: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = stored(monkeypatch)
    rows = type(row)._default_manager.filter(pk=row.pk)
    kept = stored_row(rows)
    assert {label.partition(":")[0] for label in audit()} == {row._meta.db_table}

    with pytest.raises(ValueError, match=refusal):
        forward()

    assert not any(checks(table) & NEW_CHECKS[table] for table in PLANT_TABLES)
    assert stored_row(rows) == kept
    rows.update(**correction)
    forward()
    assert all(NEW_CHECKS[table] <= checks(table) for table in PLANT_TABLES)
    assert violations("plants_calibrationrun", CALIBRATION_RUN_RULE) == 0
    assert violations("plants_plant", PLANT_AUTOMATION_RULE) == 0


def _observation(
    system: System, plant: Plant, start: datetime, **values: object
) -> ProductionObservation:
    return ProductionObservation.objects.create(
        plant=plant,
        configuration=PlantConfiguration.objects.get(plant=plant),
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        energy_mwh=1,
        source="manual",
        recorded_by=system.member,
        **values,
    )


@pytest.mark.usefixtures("leaf")
def test_observation_migration_refuses_a_revision_of_another_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start = NOW - timedelta(days=2)
    earlier = _observation(system, plant, start)
    later = _observation(system, plant, start + timedelta(hours=1))
    migrate(PREVIOUS["plants"])
    linked = ProductionObservation.objects.filter(pk=later.pk)
    linked.update(supersedes=earlier)
    assert violations("plants_productionobservation", OBSERVATION_LINEAGE_RULE) == 1

    with pytest.raises(
        ValueError, match=r"plants_productionobservation holds 1 row.*same plant and interval"
    ):
        forward()

    assert "productionobservation_revision_guard" not in triggers()
    assert linked.values_list("supersedes_id", flat=True).get() == earlier.pk
    linked.update(supersedes=None)
    forward()
    assert "productionobservation_revision_guard" in triggers()
    assert violations("plants_productionobservation", OBSERVATION_LINEAGE_RULE) == 0


@pytest.mark.usefixtures("leaf")
@pytest.mark.parametrize(
    ("model", "values", "rule"),
    [
        (ProviderAccount, {"daily_quota": 0}, "daily quota"),
        (ProviderAccount, {"daily_quota": 11}, "daily quota"),
        (ProviderAccount, {"credential_revision": 0}, "credential revision"),
        (ProviderAccount, {"authentication_failed_revision": 0}, "authentication failure"),
        (
            ProviderAccount,
            {"authentication_failed_revision": F("credential_revision") + 1},
            "authentication failure",
        ),
        (SiteBinding, {"validated_at": None}, "validation"),
        (SiteBinding, {"validated_credential_revision": None}, "validation"),
        (SiteBinding, {"validated_credential_revision": 0}, "validation"),
    ],
)
def test_provider_migration_refuses_accounts_and_bindings_no_writer_produces(
    model: type[ProviderAccount | SiteBinding], values: dict[str, object], rule: str
) -> None:
    system = make_system(approved=True)
    row = system.account if model is ProviderAccount else system.binding
    rows = model._default_manager.filter(pk=row.pk)
    stored: dict[str, object] = dict(rows.values().get())
    migrate(PREVIOUS["connections"])
    rows.update(**values)
    changed = row_values(rows)
    table = model._meta.db_table
    assert {label.partition(":")[0] for label in audit()} == {table}

    with pytest.raises(ValueError, match=rf"{table} holds 1 row.*{rule}"):
        forward()

    assert not checks(table) & NEW_CHECKS[table]
    assert row_values(rows) == changed
    rows.update(**{field: stored[field] for field in values})
    forward()
    assert NEW_CHECKS[table] <= checks(table)


def _delivery(system: System) -> NotificationDelivery:
    return NotificationDelivery.objects.create(
        schedule=ReminderSchedule.objects.create(region=system.region, effective_from=NOW),
        scheduled_for=NOW,
        available_at=NOW,
        binding=system.binding,
        preparation_deadline=NOW,
        status="failed",
        last_error="mail_rejected",
        prepared_message=UNAVAILABLE,
    )


def _collection_job(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    system = make_system()
    return CollectionJob.objects.create(
        binding=system.binding, scheduled_for=NOW, target_date=NOW.date(), available_at=NOW
    )


def _epias_job(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    connected(monkeypatch)
    return EpiasCollectionJob.objects.order_by("pk")[0]


def _calibration_run(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return request_run(*make_plant(monkeypatch))


def _notification_delivery(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return _delivery(make_system())


TOKEN = uuid.uuid4()
OWNERLESS = {"lease_token": None, "lease_expires_at": NOW + timedelta(days=1)}
FREE = {"lease_token": None, "lease_expires_at": None}


@pytest.mark.usefixtures("leaf")
@pytest.mark.parametrize(
    ("row", "stored", "correction"),
    [
        (_collection_job, {"status": "running"} | OWNERLESS, {"status": "pending"} | FREE),
        (_collection_job, {"lease_token": TOKEN, "lease_expires_at": NOW}, FREE),
        (_collection_job, {"status": "stale", "lease_token": TOKEN}, FREE),
        (_epias_job, {"status": "running"} | OWNERLESS, {"status": "pending"} | FREE),
        (_epias_job, {"status": "failed", "lease_token": TOKEN, "lease_expires_at": NOW}, FREE),
        (_calibration_run, {"status": "running"} | OWNERLESS, {"status": "pending"} | FREE),
        (_calibration_run, {"lease_expires_at": NOW}, FREE),
        (_notification_delivery, {"status": "sending"} | OWNERLESS, {"status": "failed"} | FREE),
        (_notification_delivery, {"status": "uncertain", "lease_token": TOKEN}, FREE),
        (_notification_delivery, {"lease_token": TOKEN, "lease_expires_at": NOW}, FREE),
    ],
)
def test_lease_migrations_refuse_leases_no_writer_produces(
    row: Callable[[pytest.MonkeyPatch], models.Model],
    stored: dict[str, object],
    correction: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = row(monkeypatch)
    rows = type(created)._default_manager.filter(pk=created.pk)
    table = created._meta.db_table
    check = f"{table.split('_', 1)[1]}_lease_chk"
    migrate(*PREVIOUS.values())
    rows.update(**stored)
    kept = stored_row(rows)
    assert set(audit()) == {f"{table}: {LEASE_RULES[table]}"}

    with pytest.raises(ValueError, match=rf"{table} holds 1 row.*lease"):
        forward()

    assert check not in checks(table)
    assert stored_row(rows) == kept
    rows.update(**correction)
    forward()
    assert check in checks(table)
    assert lease_violations() == LEASES_CLEAN


@pytest.mark.usefixtures("leaf")
def test_binding_migration_refuses_two_bindings_of_one_region_revision() -> None:
    system = make_system()
    duplicate = SiteBinding.objects.get(pk=system.binding.pk)
    duplicate.pk = None
    duplicate.region_revision += 1
    duplicate.save()
    migrate(PREVIOUS["connections"])
    SiteBinding.objects.filter(pk=duplicate.pk).update(
        region_revision=system.binding.region_revision
    )
    assert set(audit()) == {f"connections_sitebinding: {BINDING_REVISION_RULE}"}

    with pytest.raises(ValueError, match=r"connections_sitebinding holds 2 row.*one binding per"):
        forward()

    assert "sitebinding_region_region_revision_uniq" not in constraints("connections_sitebinding")
    assert SiteBinding.objects.filter(region=system.region).count() == 2
    SiteBinding.objects.filter(pk=duplicate.pk).update(region_revision=F("region_revision") + 1)
    forward()
    assert "sitebinding_region_region_revision_uniq" in constraints("connections_sitebinding")


@pytest.mark.usefixtures("leaf")
@pytest.mark.parametrize(
    ("values", "rule"),
    [
        ({"endpoint": "forecasts"}, "historical import endpoint"),
        ({"status": "pending"}, "succeeded import"),
    ],
)
def test_collection_migration_refuses_reference_imports_of_another_shape(
    values: dict[str, str], rule: str
) -> None:
    system = make_system()
    record = HistoricalReferenceImport.objects.create(
        binding=system.binding,
        content_sha256="a" * 64,
        source_name="fixture",
        source_settings={},
        imported_by=system.admin,
        created_at=NOW,
        interval_count=1,
    )
    job = CollectionJob.objects.create(
        binding=system.binding,
        reference_import=record,
        endpoint="historical_import",
        scheduled_for=NOW,
        target_date=NOW.date(),
        available_at=NOW,
        status="succeeded",
    )
    migrate(PREVIOUS["forecasts"])
    CollectionJob.objects.filter(pk=job.pk).update(**values)
    assert set(audit()) == {f"forecasts_collectionjob: {IMPORT_SHAPE_RULE}"}

    with pytest.raises(ValueError, match=rf"forecasts_collectionjob holds 1 row.*{rule}"):
        forward()

    assert not checks("forecasts_collectionjob") & NEW_CHECKS["forecasts_collectionjob"]
    assert CollectionJob.objects.filter(pk=job.pk, **values).exists()
    CollectionJob.objects.filter(pk=job.pk).update(endpoint="historical_import", status="succeeded")
    forward()
    assert NEW_CHECKS["forecasts_collectionjob"] <= checks("forecasts_collectionjob")


@pytest.mark.usefixtures("leaf")
def test_normalization_migration_refuses_a_factor_that_disagrees_with_its_capacities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    actual_snapshot_for(system, date(2026, 11, 7))
    normalize(system, target)
    migrate(PREVIOUS["forecasts"])
    ReferenceCapacityNormalization.objects.update(factor=F("factor") * 2)
    assert set(audit()) == {
        f"forecasts_referencecapacitynormalization: {NORMALIZATION_FACTOR_RULE}"
    }

    with pytest.raises(
        ValueError, match=r"forecasts_referencecapacitynormalization holds 1 row.*factor"
    ):
        forward()

    ReferenceCapacityNormalization.objects.update(factor=F("factor") / 2)
    forward()
    assert "referencecapacitynormalization_target_over_source_chk" in checks(
        "forecasts_referencecapacitynormalization"
    )


LARGEST = sys.float_info.max
SMALLEST = math.ulp(0.0)
# (source, target, factor) at the edges of float8: subnormal and overflowing quotients, and
# products or tolerances that leave float8 although the quotient is representable.
FLOAT_LIMITS = (
    (1e308, 1e-10, 1e-10 / 1e308),
    (1e308, 1e-10, math.nextafter(1e-10 / 1e308, 1)),
    (1e308, sys.float_info.min, SMALLEST),
    (4.0, SMALLEST, SMALLEST),
    (1.5, SMALLEST, SMALLEST / 1.5),
    (1.0, 1e-320, 1e-320),
    (3.0, LARGEST, LARGEST / 3),
    (0.5, 6e307, 6e307 / 0.5),
    (math.nextafter(1.0, 0), LARGEST, LARGEST),
    (0.5, LARGEST, 1.0),
    (1e-10, 1e300, 1.0),
    (2.0, 3.0, 1.5),
    (2.0, 3.0, math.nextafter(1.5, 2)),
)


@pytest.mark.usefixtures("leaf")
def test_the_factor_count_is_the_exact_quotient_and_never_fails_at_the_float_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    actual_snapshot_for(system, date(2026, 11, 7))
    normalize(system, target)
    rows = ReferenceCapacityNormalization.objects.all()
    migrate(PREVIOUS["forecasts"])

    for source, capacity, factor in FLOAT_LIMITS:
        rows.update(source_capacity_kw=source, target_capacity_kw=capacity, factor=factor)
        expected = int(factor != capacity / source)
        assert (
            violations("forecasts_referencecapacitynormalization", NORMALIZATION_FACTOR_RULE)
            == expected
        ), (source, capacity, factor)

    rows.update(source_capacity_kw=0.5, target_capacity_kw=LARGEST, factor=1.0)
    with pytest.raises(
        ValueError, match=r"forecasts_referencecapacitynormalization holds 1 row.*factor"
    ):
        forward()
    rows.update(source_capacity_kw=1e308, target_capacity_kw=1e-10, factor=1e-10 / 1e308)
    forward()
    assert "referencecapacitynormalization_target_over_source_chk" in checks(
        "forecasts_referencecapacitynormalization"
    )
