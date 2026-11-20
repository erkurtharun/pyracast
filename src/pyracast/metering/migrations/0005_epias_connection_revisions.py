import sys
from typing import ClassVar

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation

INTEGER_MAX = 2**31 - 1
BIGINT_MAX = 2**63 - 1


def _identifier(value: object, maximum: int) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= maximum
        else None
    )


def copy_connection_events(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """One revision per stored epias_connected event, at the event's time; the events stay as
    history. An event without a usable identity or connection, or naming a revision already
    stored with another identity, is skipped and counted."""
    alias = schema_editor.connection.alias
    connections = dict(
        apps.get_model("metering", "EpiasConnection")
        ._default_manager.using(alias)
        .values_list("plant_id", "pk")
    )
    revisions = apps.get_model("metering", "EpiasConnectionRevision")._default_manager.using(alias)
    events = (
        apps.get_model("plants", "PlantEvent")
        ._default_manager.using(alias)
        .filter(action="epias_connected")
        .order_by("created_at", "pk")
        .values_list("plant_id", "details", "created_at")
    )
    written = skipped = 0
    for plant_id, details, created_at in events.iterator():
        identity = details if isinstance(details, dict) else {}
        connection_id = connections.get(plant_id)
        revision = _identifier(identity.get("revision"), INTEGER_MAX)
        provider_plant_id = _identifier(identity.get("provider_plant_id"), BIGINT_MAX)
        eic = identity.get("eic")
        if (
            connection_id is None
            or revision is None
            or provider_plant_id is None
            or not isinstance(eic, str)
            or not 0 < len(eic) <= 100
        ):
            skipped += 1
            continue
        stored, created = revisions.get_or_create(
            connection_id=connection_id,
            revision=revision,
            defaults={
                "provider_plant_id": provider_plant_id,
                "eic": eic,
                "connected_at": created_at,
            },
        )
        if created:
            written += 1
        elif (stored.provider_plant_id, stored.eic) != (provider_plant_id, eic):
            skipped += 1
    sys.stdout.write(f"\n  EPIAS connection revisions: {written} written, {skipped} skipped.\n")


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("metering", "0004_schema_naming_convention"),
        ("plants", "0005_calibration_preparation_due_at"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="EpiasConnectionRevision",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("revision", models.PositiveIntegerField()),
                ("provider_plant_id", models.PositiveBigIntegerField()),
                ("eic", models.CharField(max_length=100)),
                ("connected_at", models.DateTimeField()),
                (
                    "connection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="metering.epiasconnection",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("connection", "revision"),
                        name="epiasconnectionrevision_connection_revision_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("revision__gte", 1)),
                        name="epiasconnectionrevision_revision_chk",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("provider_plant_id__gte", 1)),
                        name="epiasconnectionrevision_provider_plant_id_chk",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("eic", ""), _negated=True),
                        name="epiasconnectionrevision_eic_chk",
                    ),
                ],
            },
        ),
        migrations.RunPython(copy_connection_events, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="epiascollectionjob",
            name="epiascollectionjob_last_error_chk",
        ),
        migrations.AlterField(
            model_name="epiascollectionjob",
            name="last_error",
            field=models.CharField(
                blank=True,
                choices=[
                    ("retry_window_expired", "Retry window expired"),
                    ("attempts_exhausted", "Attempts exhausted"),
                    ("epias_provider_failure", "Epias provider failure"),
                    ("epias_credentials_missing", "EPIAS credentials missing"),
                    ("epias_credentials_rejected", "EPIAS credentials rejected"),
                    ("unpublished_data", "Unpublished data"),
                    ("partial_data", "Partial data"),
                ],
                default="",
                max_length=40,
            ),
        ),
        migrations.AddConstraint(
            model_name="epiascollectionjob",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "last_error__in",
                        [
                            "",
                            "retry_window_expired",
                            "attempts_exhausted",
                            "epias_provider_failure",
                            "epias_credentials_missing",
                            "epias_credentials_rejected",
                            "unpublished_data",
                            "partial_data",
                        ],
                    )
                ),
                name="epiascollectionjob_last_error_chk",
            ),
        ),
    ]
