import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (("plants", "0003_plant_deleted_at"),)

    operations = (
        migrations.AddField(
            model_name="calibrationrun",
            name="comparison_activation",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="comparison_runs",
                to="plants.calibrationactivation",
            ),
        ),
    )
