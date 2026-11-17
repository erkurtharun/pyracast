from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (("forecasts", "0019_repair_unused_quota_wait_attempts"),)

    operations = (
        migrations.AddField(
            model_name="dispatcherstatus",
            name="duration_ms",
            field=models.PositiveIntegerField(null=True),
        ),
        migrations.AddField(
            model_name="dispatcherstatus",
            name="published",
            field=models.PositiveIntegerField(null=True),
        ),
    )
