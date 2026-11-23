from typing import ClassVar

import django.contrib.postgres.fields
import django.db.models.deletion
import django.db.models.lookups
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import pyracast.notifications.models
from pyracast import schema_objects

ROLES = [("to", "To"), ("cc", "Cc"), ("bcc", "Bcc")]


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("accounts", "0008_invitation_revocation_reason_and_acceptor"),
        ("notifications", "0010_delivery_preparation_metadata"),
    ]

    # Existing schedules stay hidden, typed rows carry no choice, delivery rows were blind copies.
    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="reminderschedule",
            name="hide_recipients",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="reminderrecipient",
            name="role",
            field=models.CharField(blank=True, choices=ROLES, default="", max_length=3),
        ),
        migrations.AddField(
            model_name="reminderrecipient",
            name="role_members",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.BigIntegerField(), blank=True, default=list, size=None
            ),
        ),
        migrations.AddConstraint(
            model_name="reminderrecipient",
            constraint=models.CheckConstraint(
                condition=models.Q(role__in=["", "to", "cc", "bcc"]),
                name="reminderrecipient_role_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="reminderrecipient",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    django.db.models.lookups.Exact(
                        pyracast.notifications.models.Cardinality("role_members"), 0
                    ),
                    models.Q(
                        django.db.models.lookups.Exact(
                            pyracast.notifications.models.Dimensions("role_members"), 1
                        ),
                        pyracast.notifications.models.AllPositive("role_members"),
                        ("role__in", ["to", "cc"]),
                    ),
                    _connector="OR",
                ),
                name="reminderrecipient_role_members_chk",
            ),
        ),
        migrations.AddField(
            model_name="deliveryrecipient",
            name="role",
            field=models.CharField(choices=ROLES, default="bcc", max_length=3),
        ),
        migrations.AddConstraint(
            model_name="deliveryrecipient",
            constraint=models.CheckConstraint(
                condition=models.Q(role__in=["to", "cc", "bcc"]),
                name="deliveryrecipient_role_chk",
            ),
        ),
        migrations.CreateModel(
            name="ReminderMemberRole",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("role", models.CharField(choices=ROLES, max_length=3)),
                (
                    "membership",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="accounts.membership"
                    ),
                ),
                (
                    "schedule",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="member_roles",
                        to="notifications.reminderschedule",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("schedule", "membership"),
                        name="remindermemberrole_schedule_membership_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(role__in=["to", "cc", "bcc"]),
                        name="remindermemberrole_role_chk",
                    ),
                ],
            },
        ),
        *schema_objects.operations("notifications", "0011_recipient_roles"),
    ]
