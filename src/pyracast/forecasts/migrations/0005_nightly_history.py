from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = (
        ("forecasts", "0004_collectionschedule_forecastsnapshot_available_at_and_more"),
    )

    operations = (
        migrations.RemoveConstraint(
            model_name="collectionjob", name="unique_history_collection_day"
        ),
        migrations.AddField(
            model_name="collectionjob", name="retry_until", field=models.DateTimeField(null=True)
        ),
    )
