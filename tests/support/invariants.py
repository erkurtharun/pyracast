from typing import Any

from django.db import connection, models
from psycopg import sql

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
SITE_BINDING_RULE = "(validated_at IS NULL) = (validated_credential_revision IS NULL)"
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
    inserting it again under the same key."""
    key = row.pk
    row.delete()
    for name, value in changes.items():
        setattr(row, name, value)
    row.pk = key
    row.save(force_insert=True)
    return row


def lease_violations() -> dict[str, int]:
    return {table: violations(table, rule) for table, rule in LEASE_RULES.items()}
