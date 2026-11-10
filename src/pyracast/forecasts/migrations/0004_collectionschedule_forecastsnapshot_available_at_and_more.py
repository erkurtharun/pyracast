import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (
        ("connections", "0005_sitebinding_settings_verification_reference"),
        ("forecasts", "0003_alter_requestattempt_binding"),
        ("regions", "0002_account_audit_targets"),
    )

    operations = (
        migrations.CreateModel(
            name="CollectionSchedule",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("enabled", models.BooleanField(default=True)),
                ("times", models.JSONField(default=list)),
                ("effective_from", models.DateTimeField()),
            ],
        ),
        migrations.AddField(
            model_name="forecastsnapshot",
            name="available_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="forecastsnapshot",
            name="coverage_end",
            field=models.DateTimeField(db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="forecastsnapshot",
            name="coverage_start",
            field=models.DateTimeField(db_index=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="collectionjob",
            constraint=models.UniqueConstraint(
                condition=models.Q(("endpoint", "estimated_actuals")),
                fields=("binding", "target_date"),
                name="unique_history_collection_day",
            ),
        ),
        migrations.AddField(
            model_name="collectionschedule",
            name="region",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE, to="regions.region"
            ),
        ),
    )
