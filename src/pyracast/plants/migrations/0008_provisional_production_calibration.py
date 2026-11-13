from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (("plants", "0007_calibration_incident_evidence"),)

    operations = (
        migrations.RenameField(
            model_name="calibrationrun",
            old_name="settlement_snapshot_ids",
            new_name="production_snapshot_ids",
        ),
        migrations.AddField(
            model_name="calibrationrun",
            name="measurement_selection_policy",
            field=models.CharField(default="manual-over-settlement-hour-v1", max_length=80),
        ),
        migrations.AlterField(
            model_name="calibrationrun",
            name="measurement_selection_policy",
            field=models.CharField(
                default="manual-over-settlement-over-provisional-hour-v2", max_length=80
            ),
        ),
    )
