from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (("plants", "0002_calibrationrun_observation_id_cutoff"),)

    operations = (
        migrations.AddField(
            model_name="plant",
            name="deleted_at",
            field=models.DateTimeField(db_index=True, null=True),
        ),
    )
