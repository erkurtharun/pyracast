from typing import Any, cast

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError


def preserve_deliveries(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    alias = schema_editor.connection.alias
    members = apps.get_model("accounts", "Membership")
    emails = apps.get_model("account", "EmailAddress")
    schedules = apps.get_model("notifications", "ReminderSchedule")
    deliveries = cast(Any, apps).get_model("notifications", "NotificationDelivery")
    for schedule in schedules.objects.using(alias).all().iterator():
        known: dict[str, list[int]] = {}
        for member in (
            members.objects.using(alias)
            .filter(tenant_id=schedule.region.tenant_id)
            .select_related("user")
        ):
            address = member.user.email.strip().casefold()
            if address:
                known.setdefault(address, []).append(member.pk)
            for email in emails.objects.using(alias).filter(user_id=member.user_id, verified=True):
                key = email.email.casefold()
                if member.pk not in known.setdefault(key, []):
                    known[key].append(member.pk)
        schedule.recipient_memberships = {
            address.casefold(): known[address.casefold()]
            for address in schedule.recipients
            if address.casefold() in known
        }
        schedule.save(using=alias, update_fields=("recipient_memberships",))
        for delivery in deliveries.objects.using(alias).filter(schedule_id=schedule.pk).iterator():
            delivery.recipients = [delivery.recipient] if delivery.recipient else []
            delivery.recipient_memberships = {
                address.casefold(): known[address.casefold()]
                for address in delivery.recipients
                if address.casefold() in known
            }
            delivery.save(using=alias, update_fields=("recipients", "recipient_memberships"))


def restore_single_recipients(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    alias = schema_editor.connection.alias
    schedules = apps.get_model("notifications", "ReminderSchedule")
    deliveries = cast(Any, apps).get_model("notifications", "NotificationDelivery")
    if schedules.objects.using(alias).filter(include_workspace_members=True).exists():
        raise IrreversibleError(
            "Workspace recipient groups require restoring the pre-release backup."
        )
    if deliveries.objects.using(alias).exclude(revoked_recipients=[]).exists():
        raise IrreversibleError(
            "Revoked delivery recipients require restoring the pre-release backup."
        )
    slots: set[tuple[int, object, str]] = set()
    for delivery in deliveries.objects.using(alias).all().iterator():
        if len(delivery.recipients) > 1:
            raise IrreversibleError(
                "Grouped delivery history requires restoring the pre-release backup."
            )
        recipient = delivery.recipients[0] if delivery.recipients else ""
        key = (delivery.schedule_id, delivery.scheduled_for, recipient)
        if key in slots:
            raise IrreversibleError("Delivery slots cannot be represented by the previous schema.")
        slots.add(key)
        delivery.recipient = recipient
        delivery.save(using=alias, update_fields=("recipient",))


class Migration(migrations.Migration):
    dependencies = (
        ("notifications", "0008_shared_reminder_schedules"),
        ("account", "0001_initial"),
    )

    operations = (
        migrations.RemoveConstraint(
            model_name="notificationdelivery", name="unique_reminder_recipient_slot"
        ),
        migrations.AddField(
            model_name="notificationdelivery",
            name="grouped",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="notificationdelivery",
            name="recipient_memberships",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="notificationdelivery",
            name="recipients",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="notificationdelivery",
            name="revoked_recipients",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="reminderschedule",
            name="include_workspace_members",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="reminderschedule",
            name="recipient_memberships",
            field=models.JSONField(default=dict),
        ),
        migrations.RunPython(preserve_deliveries, restore_single_recipients),
        migrations.AlterField(
            model_name="notificationdelivery",
            name="grouped",
            field=models.BooleanField(default=True),
        ),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.UniqueConstraint(
                condition=models.Q(grouped=True),
                fields=("schedule", "scheduled_for"),
                name="unique_reminder_group_slot",
            ),
        ),
        migrations.RemoveField(model_name="notificationdelivery", name="recipient"),
    )
