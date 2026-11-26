import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Event
from typing import Any

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection, connections, transaction
from psycopg import sql

from pyracast.connections.secrets import api_key_fingerprints, decrypt_api_key
from pyracast.forecasts.models import HistoricalReferenceImport
from pyracast.metering.models import EpiasCollectionJob, EpiasSnapshot
from pyracast.plants.models import MountingType
from pyracast.plants.operations import create_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.regions.operations import submit_region
from tests.support.change_request_boundaries import (
    approve,
    incident_request,
    make_request_case,
    production_request,
)
from tests.support.domain import NOW
from tests.support.metering import FixtureClient, connect
from tests.support.migrations import constraints, forward, migrate
from tests.support.plant_operations import request_run, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = (
    ("accounts", "0004_invitation_and_administrator_change_checks"),
    ("change_requests", "0003_decision_reason_and_incident_interval_checks"),
    ("connections", "0003_provider_account_and_binding_checks"),
    ("forecasts", "0003_lease_import_and_archive_checks"),
    ("incidents", "0002_incident_kind_choices"),
    ("metering", "0003_lease_check_and_snapshot_trigger"),
    ("notifications", "0004_recipient_and_time_shape_checks"),
    ("plants", "0003_lease_check_and_observation_trigger"),
    ("regions", "0002_audit_event_details"),
)
NAMING = (
    ("accounts", "0005_schema_naming_convention"),
    ("change_requests", "0004_schema_naming_convention"),
    ("connections", "0004_schema_naming_convention"),
    ("forecasts", "0004_schema_naming_convention"),
    ("incidents", "0003_schema_naming_convention"),
    ("metering", "0004_schema_naming_convention"),
    ("notifications", "0005_schema_naming_convention"),
    ("plants", "0004_schema_naming_convention"),
    ("regions", "0003_schema_naming_convention"),
)
COLUMNS = {
    "change_requests_changerequest": {"submitted_at": "created_at"},
    "forecasts_collectionjob": {"scheduled_slot": "scheduled_for"},
    "forecasts_historicalreferenceimport": {
        "sha256": "content_sha256",
        "imported_at": "created_at",
    },
    "incidents_incidentrevision": {"recorded_at": "created_at"},
    "metering_epiascollectionjob": {"target_day": "target_date", "next_attempt_at": "available_at"},
    "metering_epiassnapshot": {"target_day": "target_date"},
    "plants_calibrationrun": {"input_fingerprint": "input_sha256"},
    "plants_plantconfiguration": {
        "surface_tilt": "surface_tilt_degrees",
        "surface_azimuth": "surface_azimuth_degrees",
    },
    "plants_plantsubmission": {
        "surface_tilt": "surface_tilt_degrees",
        "surface_azimuth": "surface_azimuth_degrees",
    },
    "plants_productionobservation": {"recorded_at": "created_at"},
}
ADDED = {"forecasts_requestattempt": {"http_status": None}}
# notifications.0007 moves the address lists into recipient rows.
RECIPIENT_LISTS = {
    "notifications_reminderschedule": {"recipients", "recipient_memberships"},
    "notifications_notificationdelivery": {
        "recipients",
        "recipient_memberships",
        "revoked_recipients",
    },
}
RECIPIENT_TABLES = (
    "notifications_deliveryrecipient",
    "notifications_reminderrecipient",
    "notifications_reminderrecipienttie",
)
NAME = r'"?\w+"?'
RENAME = re.compile(
    rf"ALTER TABLE {NAME} RENAME (?:COLUMN|CONSTRAINT) {NAME} TO {NAME}"
    rf"|ALTER INDEX {NAME} RENAME TO {NAME}"
)
QUOTED = r'"\w+"'
RENAME_IN_PLACE = re.compile(
    rf"ALTER TABLE ({QUOTED}) RENAME CONSTRAINT {QUOTED} TO {QUOTED}"
    rf"|ALTER INDEX {QUOTED} RENAME TO {QUOTED}"
)
LOCK = re.compile(rf"LOCK TABLE ({QUOTED}) IN ACCESS EXCLUSIVE MODE")


def statements(app: str, name: str, *, backwards: bool = False) -> list[str]:
    output = StringIO()
    call_command("sqlmigrate", app, name, backwards=backwards, stdout=output)
    lines = [line for line in output.getvalue().splitlines() if not line.startswith("--")]
    return [part.strip() for part in " ".join(lines).split(";") if part.strip()]


@pytest.mark.parametrize(("app", "name"), NAMING)
@pytest.mark.parametrize("backwards", [False, True])
def test_naming_migrations_lock_each_table_before_they_rename_in_it(
    app: str, name: str, backwards: bool
) -> None:
    issued = [
        statement
        for statement in statements(app, name, backwards=backwards)
        if statement not in {"BEGIN", "COMMIT"}
    ]
    locks = [index for index, statement in enumerate(issued) if LOCK.fullmatch(statement)]
    in_place = [
        index for index, statement in enumerate(issued) if RENAME_IN_PLACE.fullmatch(statement)
    ]
    assert in_place
    assert [index + 1 for index in locks] == in_place
    assert [
        statement
        for index, statement in enumerate(issued)
        if index not in locks and not RENAME.fullmatch(statement)
    ] == []
    for index in in_place:
        renamed_in = RENAME_IN_PLACE.fullmatch(issued[index])
        locked = LOCK.fullmatch(issued[index - 1])
        assert renamed_in
        assert locked
        assert renamed_in[1] in {None, locked[1]}


def lock_requests(pid: int, *relations: int) -> set[tuple[str, str, bool]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relation::regclass::text, mode, granted FROM pg_locks "
            "WHERE pid = %s AND relation = ANY(%s)",
            [pid, list(relations)],
        )
        return set(cursor.fetchall())


def scalar(query: str, *params: int) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        row = cursor.fetchone()
    assert row is not None
    return row[0]


@pytest.mark.usefixtures("leaf")
def test_an_index_rename_waits_for_its_table_before_it_locks_the_index() -> None:
    migrate(("regions", "0002_audit_event_details"))
    table = scalar("SELECT 'regions_region'::regclass::oid")
    index = scalar("SELECT 'one_active_region_per_tenant_location'::regclass::oid")
    started, migrating_pid = Event(), []

    def migrating() -> None:
        try:
            migrating_pid.append(scalar("SELECT pg_backend_pid()"))
            started.set()
            migrate(("regions", "0003_schema_naming_convention"))
        finally:
            connections.close_all()

    with ThreadPoolExecutor(1) as pool:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("LOCK TABLE regions_region IN SHARE UPDATE EXCLUSIVE MODE")
            holder = scalar("SELECT pg_backend_pid()")
            running = pool.submit(migrating)
            assert started.wait(timeout=10)
            waiter = migrating_pid[0]
            deadline = time.monotonic() + 10
            while not running.done() and not scalar(
                "SELECT %s = ANY(pg_blocking_pids(%s))", holder, waiter
            ):
                assert time.monotonic() < deadline
                time.sleep(0.02)
            assert lock_requests(waiter, table, index) == {
                ("regions_region", "AccessExclusiveLock", False)
            }
        running.result(timeout=20)

    assert "region_tenant_location_uniq" in constraints("regions_region")


def columns(table: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        )
        return {name for (name,) in cursor.fetchall()}


def catalog() -> dict[str, set[tuple[Any, ...]]]:
    queries = {
        "columns": """
            SELECT table_name, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns WHERE table_schema = current_schema()
        """,
        "constraints": """
            SELECT t.relname, c.conname, c.contype, pg_get_constraintdef(c.oid),
                c.condeferrable, c.condeferred, c.convalidated
            FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace ns ON ns.oid = t.relnamespace
            WHERE ns.nspname = current_schema()
        """,
        "indexes": """
            SELECT tablename, indexname, indexdef FROM pg_indexes
            WHERE schemaname = current_schema()
        """,
        "triggers": """
            SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid), p.prosrc
            FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_proc p ON p.oid = t.tgfoid
            JOIN pg_namespace ns ON ns.oid = c.relnamespace
            WHERE ns.nspname = current_schema() AND NOT t.tgisinternal
        """,
    }
    found: dict[str, set[tuple[Any, ...]]] = {}
    with connection.cursor() as cursor:
        for kind, query in queries.items():
            cursor.execute(query)
            found[kind] = set(cursor.fetchall())
    return found


def project_rows() -> dict[str, list[dict[str, Any]]]:
    """Every row of the project tables that exist in the migrated state."""
    existing = set(connection.introspection.table_names())
    tables = sorted(
        {
            model._meta.db_table
            for model in apps.get_models(include_auto_created=True)
            if model.__module__.startswith("pyracast.")
        }
        & existing
    )
    rows: dict[str, list[dict[str, Any]]] = {}
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute(
                sql.SQL("SELECT to_jsonb(stored) FROM {} AS stored").format(sql.Identifier(table))
            )
            rows[table] = sorted((json.loads(row) for (row,) in cursor.fetchall()), key=repr)
    return rows


def renamed(rows: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    return {
        table: sorted(
            (
                {
                    **{COLUMNS.get(table, {}).get(key, key): value for key, value in row.items()},
                    **ADDED.get(table, {}),
                }
                for row in stored
            ),
            key=repr,
        )
        for table, stored in rows.items()
    }


def populate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyracast.metering.scheduler.get_epias_client", lambda: FixtureClient())
    case = make_request_case(monkeypatch)
    system = case.system
    approve(case, production_request(case))
    approve(case, incident_request(case))
    snapshot_for(system, NOW.date())
    request_run(system, case.plant)
    connection_row = connect(case.plant, system.admin)
    job = EpiasCollectionJob.objects.filter(connection=connection_row).first()
    assert job is not None
    EpiasSnapshot.objects.create(
        job=job,
        attempt=1,
        connection=connection_row,
        configuration_id=job.configuration_id,
        target_date=job.target_date,
        collected_at=NOW,
        available_at=NOW,
        raw_payload={},
        readings=[],
        content_sha256="0" * 64,
    )
    HistoricalReferenceImport.objects.create(
        binding=system.binding,
        content_sha256="1" * 64,
        source_name="export.json",
        source_settings={},
        imported_by=system.admin,
        created_at=NOW - timedelta(days=1),
        interval_count=0,
    )
    pending = submit_region(
        system.member,
        system.tenant.pk,
        name="Second site",
        latitude=41,
        longitude=30,
        timezone="Europe/Istanbul",
    )
    create_plant(
        system.member,
        system.tenant.pk,
        pending.pk,
        PlantParameters(
            name="Second site",
            dc_capacity_mw=10,
            mounting_type=MountingType.FIXED,
            surface_tilt_degrees=25,
            surface_azimuth_degrees=180,
        ),
    )


@pytest.mark.usefixtures("leaf")
def test_upgrade_keeps_every_row_and_builds_the_fresh_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = catalog()
    assert all(set(renames.values()) <= columns(table) for table, renames in COLUMNS.items())
    populate(monkeypatch)

    migrate(*PREVIOUS)
    assert all(set(renames) <= columns(table) for table, renames in COLUMNS.items())
    assert catalog() != fresh
    assert all(lists <= columns(table) for table, lists in RECIPIENT_LISTS.items())
    assert "revision" not in columns("notifications_reminderschedule")
    before = project_rows()
    assert all(before[table] for table in COLUMNS)
    assert not set(RECIPIENT_TABLES) & before.keys()

    forward()

    assert catalog() == fresh
    assert all(not lists & columns(table) for table, lists in RECIPIENT_LISTS.items())
    assert "revision" in columns("notifications_reminderschedule")
    after = project_rows()
    assert [
        (row["id"], row["calibration_preparation_due_at"] is not None)
        for row in after["plants_plant"]
    ] == [
        (row["id"], row["current_configuration_id"] is not None) for row in before["plants_plant"]
    ]
    for row in after["plants_plant"]:
        del row["calibration_preparation_due_at"]
    assert all(row.pop("parameters_revision") == 1 for row in after["plants_plant"])
    assert after["regions_region"]
    assert all(row["purge_started_at"] is None for row in after["regions_region"])
    assert [
        (row["id"], row["purge_warning_sent_at"] is not None) for row in after["regions_region"]
    ] == [(row["id"], row["deleted_at"] is not None) for row in before["regions_region"]]
    for row in after["regions_region"]:
        del row["purge_started_at"]
        del row["purge_warning_sent_at"]
    assert all(
        row.pop("purge_plant_ids") is row.pop("purge_bindings") is None
        for row in after["regions_region"]
    )
    assert all(row["updated_at"] == row["created_at"] for row in after["plants_plantsubmission"])
    for row in after["plants_plantsubmission"]:
        del row["updated_at"]
    assert after["connections_provideraccount"]
    for row in after["connections_provideraccount"]:
        assert row.pop("revalidation_due_at") is None
        key = row["encrypted_api_key"]
        assert row.pop("api_key_fingerprint") == (
            api_key_fingerprints(decrypt_api_key(key))[0] if key else None
        )
    assert all(row.pop("base_configuration_id") is None for row in after["plants_plantsubmission"])
    assert after["plants_calibrationrun"]
    assert all(row["available_at"] is None for row in after["plants_calibrationrun"])
    for row in after["plants_calibrationrun"]:
        del row["available_at"]
    assert after["incidents_plantincident"]
    assert all(row.pop("idempotency_key") is None for row in after["incidents_plantincident"])
    assert after.pop("notifications_outboundmail") == []
    assert after.pop("notifications_remindermemberrole") == []
    assert [after.pop(table) for table in RECIPIENT_TABLES] == [[], [], []]
    revisions = after.pop("metering_epiasconnectionrevision")
    assert [
        (row["connection_id"], row["revision"], row["provider_plant_id"], row["eic"])
        for row in revisions
    ] == [
        (row["id"], 1, row["provider_plant_id"], row["eic"])
        for row in before["metering_epiasconnection"]
    ]
    for row in before["forecasts_forecastsnapshot"]:
        del row["weather_payload"], row["weather_error"]
    assert after == renamed(before)
