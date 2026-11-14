from typing import ClassVar

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies: ClassVar = [("notifications", "0006_delivery_preparation")]

    operations: ClassVar = [
        migrations.AddField(
            model_name="notificationdelivery",
            name="language",
            field=models.CharField(
                choices=[("en", "English"), ("tr", "Türkçe")], default="en", max_length=2
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.CheckConstraint(
                condition=models.Q(language__in=("en", "tr")),
                name="notification_delivery_language_valid",
            ),
        ),
    ]
