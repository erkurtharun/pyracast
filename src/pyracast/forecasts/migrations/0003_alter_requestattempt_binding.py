import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (
        ("connections", "0005_sitebinding_settings_verification_reference"),
        ("forecasts", "0002_forecastsnapshot_source_kind"),
    )

    operations = (
        migrations.AlterField(
            model_name="requestattempt",
            name="binding",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT, to="connections.sitebinding"
            ),
        ),
    )
