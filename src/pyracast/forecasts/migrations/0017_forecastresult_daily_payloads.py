from django.db import migrations, models

BACKFILL = """
UPDATE forecasts_forecastresult AS result
SET daily_payloads = jsonb_build_object(job.target_date::text, result.payload)
FROM forecasts_forecastsnapshot AS snapshot
JOIN forecasts_collectionjob AS job ON job.id = snapshot.job_id
WHERE result.snapshot_id = snapshot.id
  AND result.payload->>'local_date' = job.target_date::text
"""


class Migration(migrations.Migration):
    dependencies = (("forecasts", "0016_snapshot_query_index"),)

    operations = (
        migrations.AddField(
            model_name="forecastresult",
            name="daily_payloads",
            field=models.JSONField(default=dict),
        ),
        migrations.RunSQL(BACKFILL, migrations.RunSQL.noop),
    )
