from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (("plants", "0006_calibrationrun_input_fingerprint_and_more"),)

    operations = (
        migrations.AddField(
            model_name="calibrationrun",
            name="incident_revision_ids",
            field=models.JSONField(default=list),
        ),
    )
