import sys
from datetime import datetime
from typing import Any, ClassVar

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation

KINDS = ("current", "saved_complete", "saved_partial", "unavailable")
METADATA = ("kind", "tomorrow_complete", "prepared_at")


def deliveries(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> models.QuerySet[Any]:
    model: type[models.Model] = apps.get_model("notifications", "NotificationDelivery")
    return model._default_manager.using(schema_editor.connection.alias)


def _prepared_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None


def move_metadata(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """A message without a known kind and an aware preparation time keeps its message and gets
    no columns; it is counted."""
    rows = deliveries(apps, schema_editor)
    skipped = 0
    prepared = rows.filter(prepared_message__isnull=False).order_by("pk")
    for delivery in prepared.iterator(chunk_size=100):
        message = delivery.prepared_message
        kind, prepared_at = message.get("kind"), _prepared_at(message.get("prepared_at"))
        if kind not in KINDS or prepared_at is None:
            skipped += 1
            continue
        complete = message.get("tomorrow_complete")
        rows.filter(pk=delivery.pk).update(
            forecast_kind=kind,
            forecast_complete=complete if isinstance(complete, bool) else None,
            prepared_at=prepared_at,
            prepared_message={key: value for key, value in message.items() if key not in METADATA},
        )
    if skipped:
        sys.stdout.write(
            f"\n  notifications: {skipped} prepared delivery message(s) carried no readable "
            "kind and time and kept their message unchanged."
        )


def restore_metadata(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    rows = deliveries(apps, schema_editor)
    for delivery in rows.exclude(forecast_kind="").filter(prepared_message__isnull=False):
        metadata: dict[str, object] = {
            "kind": delivery.forecast_kind,
            "prepared_at": delivery.prepared_at.isoformat(),
        }
        if delivery.forecast_complete is not None:
            metadata["tomorrow_complete"] = delivery.forecast_complete
        rows.filter(pk=delivery.pk).update(
            prepared_message={**delivery.prepared_message, **metadata}
        )


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("connections", "0004_schema_naming_convention"),
        ("forecasts", "0006_collection_outcomes_and_ledger_status"),
        ("notifications", "0009_purge_warning_mail"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="notificationdelivery",
            name="forecast_complete",
            field=models.BooleanField(null=True),
        ),
        migrations.AddField(
            model_name="notificationdelivery",
            name="forecast_kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("current", "Current forecast"),
                    ("saved_complete", "Saved complete forecast"),
                    ("saved_partial", "Saved partial forecast"),
                    ("unavailable", "Unavailable"),
                ],
                default="",
                max_length=14,
            ),
        ),
        migrations.AddField(
            model_name="notificationdelivery",
            name="prepared_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.RunPython(move_metadata, restore_metadata),
        migrations.RemoveConstraint(
            model_name="notificationdelivery",
            name="notificationdelivery_prepared_kind_chk",
        ),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.CheckConstraint(
                condition=models.Q(("forecast_kind__in", ["", *KINDS])),
                name="notificationdelivery_forecast_kind_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("forecast_complete__isnull", True),
                        ("forecast_kind", ""),
                        ("prepared_at__isnull", True),
                    ),
                    models.Q(
                        models.Q(("forecast_kind", ""), _negated=True),
                        ("prepared_at__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="notificationdelivery_prepared_at_chk",
            ),
        ),
    ]
