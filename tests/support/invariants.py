import json
from typing import Any

from django.db import connection, models, transaction
from psycopg import sql

from pyracast.change_requests.models import ChangeRequestStatus
from pyracast.forecasts.models import CollectionJobStatus
from pyracast.metering.models import EpiasJobStatus
from pyracast.notifications.models import NotificationDeliveryStatus
from pyracast.plants.models import CalibrationStatus


def _held_only_while(status: str) -> str:
    return (
        "(lease_token IS NULL) = (lease_expires_at IS NULL)"
        f" AND (lease_token IS NOT NULL) = (status = '{status}')"
    )


SENDING = NotificationDeliveryStatus.SENDING
UNCERTAIN = NotificationDeliveryStatus.UNCERTAIN

# Written independently of the CHECK definitions so that a writer audit can count violations
# on a database that does not enforce them.
LEASE_RULES = {
    "forecasts_collectionjob": _held_only_while(CollectionJobStatus.RUNNING),
    "metering_epiascollectionjob": _held_only_while(EpiasJobStatus.RUNNING),
    "plants_calibrationrun": _held_only_while(CalibrationStatus.RUNNING),
    "notifications_notificationdelivery": (
        "(lease_token IS NULL) = (lease_expires_at IS NULL)"
        f" AND (status <> '{SENDING}' OR lease_token IS NOT NULL)"
        f" AND (lease_token IS NULL OR status IN ('{SENDING}', '{UNCERTAIN}'))"
    ),
}

PROVIDER_ACCOUNT_RULE = (
    "daily_quota BETWEEN 1 AND 10 AND credential_revision >= 1"
    " AND (authentication_failed_revision IS NULL"
    " OR authentication_failed_revision BETWEEN 1 AND credential_revision)"
)
SITE_BINDING_RULE = """
(validated_at IS NULL AND validated_credential_revision IS NULL)
OR (validated_at IS NOT NULL AND validated_credential_revision IS NOT NULL
    AND validated_credential_revision >= 1)
"""
CALIBRATION_RUN_RULE = (
    f"(status IN ('{CalibrationStatus.PENDING}', '{CalibrationStatus.RUNNING}')"
    " OR completed_at IS NOT NULL)"
    " AND assessment IN ('', 'insufficient', 'provisional', 'evaluated')"
)
PLANT_AUTOMATION_RULE = (
    "(automatic_calibration_enabled AND automatic_calibration_enabled_by_id IS NOT NULL"
    " AND next_calibration_check_at IS NOT NULL)"
    " OR (NOT automatic_calibration_enabled AND automatic_calibration_enabled_by_id IS NULL"
    " AND next_calibration_check_at IS NULL)"
)
OBSERVATION_LINEAGE_RULE = (
    "supersedes_id IS NULL OR EXISTS (SELECT 1 FROM plants_productionobservation AS previous"
    " WHERE previous.id = plants_productionobservation.supersedes_id"
    " AND previous.plant_id = plants_productionobservation.plant_id"
    " AND previous.starts_at = plants_productionobservation.starts_at"
    " AND previous.ends_at = plants_productionobservation.ends_at)"
)
IMPORT_SHAPE_RULE = (
    "(endpoint = 'historical_import') = (reference_import_id IS NOT NULL)"
    " AND (reference_import_id IS NULL OR status = 'succeeded')"
)
NATIVE_SLOT_RULE = (
    "endpoint NOT IN ('forecasts', 'estimated_actuals') OR NOT EXISTS (SELECT 1"
    " FROM forecasts_collectionjob AS other WHERE other.id <> forecasts_collectionjob.id"
    " AND other.binding_id = forecasts_collectionjob.binding_id"
    " AND other.endpoint = forecasts_collectionjob.endpoint"
    " AND other.scheduled_for = forecasts_collectionjob.scheduled_for)"
)
BINDING_REVISION_RULE = (
    "NOT EXISTS (SELECT 1 FROM connections_sitebinding AS other"
    " WHERE other.id <> connections_sitebinding.id"
    " AND other.region_id = connections_sitebinding.region_id"
    " AND other.region_revision = connections_sitebinding.region_revision)"
)
# factor = target / source, evaluated without the float8 underflow or overflow errors
# PostgreSQL raises: a quotient that rounds to 0 (at most 2^-1075) or to infinity (at least
# 2^1024) breaks the rule, since a stored factor is positive and finite. The guards compare
# exact power-of-two scalings: 2.2250738585072014e-308 = 2^-1022,
# 8.98846567431158e307 = 2^1023, 4503599627370496 = 2^52, 9007199254740992 = 2^53, 5e-324 = 2^-1074.
NORMALIZATION_FACTOR_RULE = """CASE
    WHEN source_capacity_kw >= 1 THEN CASE
        WHEN target_capacity_kw < 2.2250738585072014e-308 THEN CASE
            WHEN target_capacity_kw * 8.98846567431158e307 * 4503599627370496 > source_capacity_kw
            THEN factor = target_capacity_kw / source_capacity_kw ELSE false END
        WHEN source_capacity_kw >= 9007199254740992 THEN CASE
            WHEN target_capacity_kw > source_capacity_kw * 0.5 * 5e-324
            THEN factor = target_capacity_kw / source_capacity_kw ELSE false END
        ELSE factor = target_capacity_kw / source_capacity_kw END
    WHEN target_capacity_kw < source_capacity_kw * 8.98846567431158e307
    THEN factor = target_capacity_kw / source_capacity_kw
    WHEN target_capacity_kw * 0.5 < source_capacity_kw * 8.98846567431158e307 THEN CASE
        WHEN factor >= 8.98846567431158e307
        THEN factor * 0.5 = target_capacity_kw * 0.5 / source_capacity_kw ELSE false END
    ELSE false END"""
ADMINISTRATOR_CHANGE_RULE = (
    "previous_administrator_id IS NULL OR previous_administrator_id <> administrator_id"
)
INVITATION_STATE_RULE = (
    "revoked_at IS NULL OR (accepted_at IS NULL AND NOT expires_at > revoked_at)"
)
OPEN_INVITATION_RULE = """
accepted_at IS NOT NULL OR revoked_at IS NOT NULL OR NOT EXISTS (
    SELECT 1 FROM accounts_workspaceinvitation AS other
    WHERE other.id <> accounts_workspaceinvitation.id
      AND other.tenant_id = accounts_workspaceinvitation.tenant_id
      AND other.accepted_at IS NULL AND other.revoked_at IS NULL
      AND (other.email = accounts_workspaceinvitation.email
           OR (other.role = 'admin' AND accounts_workspaceinvitation.role = 'admin')))
"""
REJECTION_REASON_RULE = f"status <> '{ChangeRequestStatus.REJECTED}' OR length(decision_reason) > 0"
INCIDENT_INTERVAL_RULE = "starts_at IS NULL OR ends_at IS NULL OR starts_at < ends_at"
# The JSON rules unpack the values with set-returning functions, apart from the jsonpath
# expressions of the CHECK constraints; CASE keeps a value of another type from raising.
RECIPIENTS_RULE = """CASE WHEN jsonb_typeof(recipients) = 'array' THEN NOT EXISTS (
    SELECT 1 FROM jsonb_array_elements(recipients) AS item WHERE jsonb_typeof(item) <> 'string')
    ELSE false END"""
REVOKED_RECIPIENTS_RULE = """CASE WHEN jsonb_typeof(revoked_recipients) = 'array'
    THEN NOT EXISTS (SELECT 1 FROM jsonb_array_elements(revoked_recipients) AS item
                     WHERE jsonb_typeof(item) <> 'string')
    ELSE false END"""
MEMBERSHIPS_RULE = """CASE WHEN jsonb_typeof(recipient_memberships) = 'object' THEN NOT EXISTS (
    SELECT 1 FROM jsonb_each(recipient_memberships) AS entry WHERE CASE
        WHEN jsonb_typeof(entry.value) = 'array' THEN EXISTS (
            SELECT 1 FROM jsonb_array_elements(entry.value) AS member WHERE CASE
                WHEN jsonb_typeof(member) = 'number'
                THEN member::numeric < 1 OR member::numeric <> trunc(member::numeric)
                ELSE true END)
        ELSE true END)
    ELSE false END"""
REMINDER_TIMES_RULE = """CASE WHEN jsonb_typeof(times) = 'array' THEN NOT EXISTS (
    SELECT 1 FROM jsonb_array_elements(times) AS item WHERE CASE
        WHEN jsonb_typeof(item) = 'string'
        THEN item #>> '{}' !~ '^(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]$'
        ELSE true END)
    ELSE false END"""
AUDIT_DETAILS_RULE = "left(details::text, 1) = '{'"
# Every row invariant the database enforces on the constrained tables; archive immutability is
# a before/after property and is audited by `rewritten_archive_rows`.
STORED_RULES: tuple[tuple[str, str], ...] = (
    *LEASE_RULES.items(),
    ("accounts_workspaceadminchange", ADMINISTRATOR_CHANGE_RULE),
    ("accounts_workspaceinvitation", INVITATION_STATE_RULE),
    ("accounts_workspaceinvitation", OPEN_INVITATION_RULE),
    ("change_requests_changerequest", REJECTION_REASON_RULE),
    ("change_requests_incidentchange", INCIDENT_INTERVAL_RULE),
    ("notifications_reminderschedule", RECIPIENTS_RULE),
    ("notifications_reminderschedule", MEMBERSHIPS_RULE),
    ("notifications_reminderschedule", REMINDER_TIMES_RULE),
    ("notifications_notificationdelivery", RECIPIENTS_RULE),
    ("notifications_notificationdelivery", REVOKED_RECIPIENTS_RULE),
    ("notifications_notificationdelivery", MEMBERSHIPS_RULE),
    ("regions_auditevent", AUDIT_DETAILS_RULE),
    ("forecasts_collectionjob", IMPORT_SHAPE_RULE),
    ("forecasts_collectionjob", NATIVE_SLOT_RULE),
    ("forecasts_referencecapacitynormalization", NORMALIZATION_FACTOR_RULE),
    ("connections_provideraccount", PROVIDER_ACCOUNT_RULE),
    ("connections_sitebinding", SITE_BINDING_RULE),
    ("connections_sitebinding", BINDING_REVISION_RULE),
    ("plants_calibrationrun", CALIBRATION_RUN_RULE),
    ("plants_plant", PLANT_AUTOMATION_RULE),
    ("plants_productionobservation", OBSERVATION_LINEAGE_RULE),
)
OBSERVATIONS = "plants_productionobservation"
FORECAST_SNAPSHOTS = "forecasts_forecastsnapshot"
# An unpublished forecast snapshot still takes its weather and its publication.
STAGED = ("available_at", "weather_payload", "weather_error")
ARCHIVES = (
    FORECAST_SNAPSHOTS,
    "forecasts_historicalreferenceimport",
    "metering_epiassnapshot",
    OBSERVATIONS,
)


def violations(table: str, rule: str) -> int:
    query = sql.SQL("SELECT count(*) FROM {} WHERE NOT ({})").format(
        sql.Identifier(table), sql.SQL(rule)
    )
    with connection.cursor() as cursor:
        cursor.execute(query)
        (count,) = cursor.fetchone()
    return int(count)


def stored_again[Row: models.Model](row: Row, **changes: Any) -> Row:
    """Archive rows refuse updates, so a test stores another shape of one by deleting it and
    inserting it again under the same key in one transaction; the keys of its dependents are
    deferred, so they still hold at commit."""
    for name, value in changes.items():
        setattr(row, name, value)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("DELETE FROM {} WHERE id = %s").format(sql.Identifier(row._meta.db_table)),
            [row.pk],
        )
        row.save(force_insert=True)
    return row


def lease_violations() -> dict[str, int]:
    return {table: violations(table, rule) for table, rule in LEASE_RULES.items()}


def stored_violations() -> dict[str, int]:
    query = sql.SQL(" UNION ALL ").join(
        sql.SQL("SELECT {}, count(*) FROM {} WHERE NOT ({})").format(
            sql.Literal(f"{table}: {rule}"), sql.Identifier(table), sql.SQL(rule)
        )
        for table, rule in STORED_RULES
    )
    with connection.cursor() as cursor:
        cursor.execute(query)
        return {label: count for label, count in cursor.fetchall() if count}


def archived_rows() -> dict[str, dict[int, dict[str, Any]]]:
    rows: dict[str, dict[int, dict[str, Any]]] = {}
    with connection.cursor() as cursor:
        for table in ARCHIVES:
            cursor.execute(
                sql.SQL("SELECT id, to_jsonb(stored) FROM {} AS stored").format(
                    sql.Identifier(table)
                )
            )
            rows[table] = {key: json.loads(row) for key, row in cursor.fetchall()}
    return rows


def superseded_revisions(
    before: dict[str, dict[int, dict[str, Any]]], after: dict[str, dict[int, dict[str, Any]]]
) -> set[int]:
    return {
        key
        for key, row in before[OBSERVATIONS].items()
        if row["is_current"] and not after[OBSERVATIONS].get(key, row)["is_current"]
    }


def rewritten_archive_rows(
    before: dict[str, dict[int, dict[str, Any]]], after: dict[str, dict[int, dict[str, Any]]]
) -> dict[str, list[int]]:
    """Archive rows stored before a writer ran that it changed or removed; a production
    revision may only leave the current set, an unpublished forecast snapshot may only take its
    weather and publication."""
    superseded = superseded_revisions(before, after)
    changed: dict[str, list[int]] = {}
    for table, rows in before.items():
        for key, row in rows.items():
            stored = after[table].get(key)
            if stored is not None and table == OBSERVATIONS and key in superseded:
                stored = {**stored, "is_current": True}
            if stored is not None and table == FORECAST_SNAPSHOTS and row["available_at"] is None:
                stored = {**stored, **{name: row[name] for name in STAGED}}
            if stored != row:
                changed.setdefault(table, []).append(key)
    return changed
