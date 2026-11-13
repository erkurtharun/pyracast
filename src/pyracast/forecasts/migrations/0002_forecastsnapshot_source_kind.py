from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (("forecasts", "0001_initial"),)

    operations = (
        migrations.AddField(
            model_name="forecastsnapshot",
            name="source_kind",
            field=models.CharField(default="unknown", max_length=20),
        ),
    )
