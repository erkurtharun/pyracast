import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (
        ("plants", "0008_provisional_production_calibration"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    )

    operations = (
        migrations.AddField(
            model_name="plant",
            name="automatic_calibration_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="plant",
            name="automatic_calibration_enabled_by",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="plant",
            name="next_calibration_check_at",
            field=models.DateTimeField(db_index=True, null=True),
        ),
        migrations.AlterField(
            model_name="calibrationactivation",
            name="actor",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    )
