from django.db import migrations

REPAIR = """
UPDATE forecasts_collectionjob AS job
SET attempts = 0
WHERE job.endpoint = 'forecasts'
  AND job.status = 'retry'
  AND job.last_error = 'quota_exhausted'
  AND job.attempts >= 3
  AND job.lease_token IS NULL
  AND job.lease_expires_at IS NULL
  AND job.reference_import_id IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM forecasts_requestattempt AS attempt WHERE attempt.job_id = job.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM forecasts_forecastsnapshot AS snapshot WHERE snapshot.job_id = job.id
  )
"""


class Migration(migrations.Migration):
    dependencies = (("forecasts", "0018_dispatcher_status"),)
    operations = (migrations.RunSQL(REPAIR, migrations.RunSQL.noop),)
