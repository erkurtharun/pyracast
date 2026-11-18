from django.db import connection
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


def violations(table: str, rule: str) -> int:
    query = sql.SQL("SELECT count(*) FROM {} WHERE NOT ({})").format(
        sql.Identifier(table), sql.SQL(rule)
    )
    with connection.cursor() as cursor:
        cursor.execute(query)
        (count,) = cursor.fetchone()
    return int(count)


def lease_violations() -> dict[str, int]:
    return {table: violations(table, rule) for table, rule in LEASE_RULES.items()}
