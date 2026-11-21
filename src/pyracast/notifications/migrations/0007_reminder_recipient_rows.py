import sys
from collections import defaultdict
from typing import ClassVar

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation

# A pin beyond the id column's range names no membership; tie 0 keeps it naming none.
LARGEST_ID = 2**63 - 1


def recipient_key(address: str) -> str:
    return address.strip().casefold()


def historical(apps: Apps, alias: str, name: str) -> models.QuerySet[models.Model]:
    model: type[models.Model] = apps.get_model("notifications", name)
    return model._default_manager.using(alias)


def copy_recipients(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Copies the lists verbatim: every schedule address with the pins of its key as ties,
    every delivery address with its revocation. A marked delivery's rows are its envelope by
    the marker, not by its revocations; no status implies an envelope. A second spelling of one
    key is kept once, with the pins of both, and counted, as are pins that name no address."""
    alias = schema_editor.connection.alias
    schedules = historical(apps, alias, "ReminderSchedule")
    deliveries = historical(apps, alias, "NotificationDelivery")
    recipients = historical(apps, alias, "ReminderRecipient")
    ties = historical(apps, alias, "ReminderRecipientTie")
    delivered = historical(apps, alias, "DeliveryRecipient")
    repeated = unbounded = orphaned = 0
    for schedule_id, addresses, pins in (
        schedules.order_by("pk").values_list("pk", "recipients", "recipient_memberships").iterator()
    ):
        pinned: dict[str, set[int]] = defaultdict(set)
        for pin_key, members in pins.items():
            pinned[recipient_key(pin_key)].update(int(pin) for pin in members)
        kept: dict[str, str] = {}
        for address in addresses:
            key = recipient_key(address)
            if key in kept:
                repeated += 1
            else:
                kept[key] = address
        orphaned += sum(len(members) for key, members in pinned.items() if key not in kept)
        for key, address in kept.items():
            members = pinned.get(key, set())
            unbounded += sum(pin > LARGEST_ID for pin in members)
            row = recipients.create(schedule_id=schedule_id, address=address, key=key)
            ties.bulk_create(
                ties.model(recipient_id=row.pk, membership_id=pin)
                for pin in sorted({pin if pin <= LARGEST_ID else 0 for pin in members})
            )
    for delivery_id, addresses, revoked in (
        deliveries.order_by("pk").values_list("pk", "recipients", "revoked_recipients").iterator()
    ):
        withdrawn = {recipient_key(address) for address in revoked}
        rows: dict[str, models.Model] = {}
        for address in addresses:
            key = recipient_key(address)
            if key in rows:
                repeated += 1
                continue
            rows[key] = delivered.model(
                delivery_id=delivery_id, address=address, key=key, revoked=key in withdrawn
            )
        delivered.bulk_create(rows.values())
    if repeated:
        sys.stdout.write(
            f"\n  notifications: {repeated} reminder address(es) repeated another spelling "
            "and were kept once."
        )
    if unbounded:
        sys.stdout.write(
            f"\n  notifications: {unbounded} membership pin(s) beyond the id range were kept "
            "as a tie that names no membership."
        )
    if orphaned:
        sys.stdout.write(
            f"\n  notifications: {orphaned} membership pin(s) named no address of their list "
            "and were not kept."
        )


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("notifications", "0006_outbound_mail"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="reminderschedule",
            name="revision",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.CreateModel(
            name="ReminderRecipient",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("address", models.TextField()),
                ("key", models.TextField()),
                (
                    "schedule",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="recipient_rows",
                        to="notifications.reminderschedule",
                    ),
                ),
            ],
            options={
                "ordering": ("pk",),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("schedule", "key"), name="reminderrecipient_schedule_key_uniq"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="ReminderRecipientTie",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("membership_id", models.PositiveBigIntegerField()),
                (
                    "recipient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ties",
                        to="notifications.reminderrecipient",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("recipient", "membership_id"),
                        name="reminderrecipienttie_recipient_membership_uniq",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="DeliveryRecipient",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("address", models.TextField()),
                ("key", models.TextField()),
                ("revoked", models.BooleanField(default=False)),
                (
                    "delivery",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="recipient_rows",
                        to="notifications.notificationdelivery",
                    ),
                ),
            ],
            options={
                "ordering": ("pk",),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("delivery", "key"), name="deliveryrecipient_delivery_key_uniq"
                    )
                ],
            },
        ),
        migrations.RunPython(copy_recipients, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="notificationdelivery",
            name="notificationdelivery_recipients_chk",
        ),
        migrations.RemoveConstraint(
            model_name="notificationdelivery",
            name="notificationdelivery_recipient_memberships_chk",
        ),
        migrations.RemoveConstraint(
            model_name="notificationdelivery",
            name="notificationdelivery_revoked_recipients_chk",
        ),
        migrations.RemoveConstraint(
            model_name="reminderschedule",
            name="reminderschedule_recipients_chk",
        ),
        migrations.RemoveConstraint(
            model_name="reminderschedule",
            name="reminderschedule_recipient_memberships_chk",
        ),
        migrations.RemoveField(
            model_name="notificationdelivery",
            name="recipient_memberships",
        ),
        migrations.RemoveField(
            model_name="notificationdelivery",
            name="recipients",
        ),
        migrations.RemoveField(
            model_name="notificationdelivery",
            name="revoked_recipients",
        ),
        migrations.RemoveField(
            model_name="reminderschedule",
            name="recipient_memberships",
        ),
        migrations.RemoveField(
            model_name="reminderschedule",
            name="recipients",
        ),
    ]
