from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("accounts", "0003_retire_legacy_otp"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="UserLanguagePreference",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "language",
                    models.CharField(choices=[("en", "English"), ("tr", "Türkçe")], max_length=2),
                ),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("language__in", ("en", "tr"))),
                        name="supported_user_language",
                    )
                ],
            },
        ),
    ]
