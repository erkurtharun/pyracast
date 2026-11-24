from typing import ClassVar

import django.core.validators
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar = [("notifications", "0011_recipient_roles")]

    operations: ClassVar[list[Operation]] = [
        migrations.RemoveConstraint(
            model_name="notificationsettings",
            name="notificationsettings_max_recipients_chk",
        ),
        migrations.AlterField(
            model_name="notificationsettings",
            name="max_recipients",
            field=models.PositiveSmallIntegerField(
                default=5,
                help_text=(
                    "Global recipient limit per plant reminder schedule. Lowering it pauses "
                    "reminders at plants exceeding the limit until their recipient lists are "
                    "revised."
                ),
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(100),
                ],
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationsettings",
            constraint=models.CheckConstraint(
                condition=models.Q(("max_recipients__gte", 1), ("max_recipients__lte", 100)),
                name="notificationsettings_max_recipients_chk",
            ),
        ),
    ]
