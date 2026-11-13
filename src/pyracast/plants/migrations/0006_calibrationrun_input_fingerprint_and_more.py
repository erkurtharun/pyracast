from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (("plants", "0005_plantconfiguration_mounting_geometry"),)

    operations = (
        migrations.AddField(
            model_name="calibrationrun",
            name="input_fingerprint",
            field=models.CharField(default="", max_length=64),
        ),
        migrations.AddField(
            model_name="calibrationrun",
            name="reference_snapshot_ids",
            field=models.JSONField(default=None, null=True),
        ),
        migrations.AddField(
            model_name="calibrationrun",
            name="settlement_snapshot_ids",
            field=models.JSONField(default=list),
        ),
    )
