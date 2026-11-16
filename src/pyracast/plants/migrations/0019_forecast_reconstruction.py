from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("forecasts", "0019_repair_unused_quota_wait_attempts"),
        ("plants", "0018_forecast_issuance"),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="ForecastReconstruction",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("target_date", models.DateField()),
                ("reconstructed_at", models.DateTimeField()),
                ("source_cutoff_at", models.DateTimeField()),
                ("model_version", models.CharField(max_length=100)),
                ("factor", models.FloatField()),
                ("payloads", models.JSONField()),
                (
                    "calibration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="plants.calibrationrun"
                    ),
                ),
                (
                    "configuration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="plants.plantconfiguration"
                    ),
                ),
                (
                    "plant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="plants.plant"
                    ),
                ),
                (
                    "snapshot",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="forecasts.forecastsnapshot"
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["plant", "target_date"], name="reconstruction_plant_day")
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=(
                            "snapshot",
                            "configuration",
                            "calibration",
                            "target_date",
                            "model_version",
                        ),
                        name="forecast_reconstruction_unique",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("factor__gt", 0), ("factor__lt", float("inf"))),
                        name="reconstruction_factor_finite",
                    ),
                ],
            },
        ),
        migrations.RunSQL(
            """
            ALTER TABLE plants_forecastreconstruction
            ADD CONSTRAINT reconstruction_configuration_owner_fk
            FOREIGN KEY (configuration_id, plant_id)
            REFERENCES plants_plantconfiguration (id, plant_id)
            DEFERRABLE INITIALLY DEFERRED;

            ALTER TABLE plants_forecastreconstruction
            ADD CONSTRAINT reconstruction_calibration_configuration_fk
            FOREIGN KEY (calibration_id, configuration_id)
            REFERENCES plants_calibrationrun (id, configuration_id)
            DEFERRABLE INITIALLY DEFERRED;

            CREATE FUNCTION guard_forecast_reconstruction() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'UPDATE' THEN
                    RAISE EXCEPTION 'Forecast reconstructions are immutable'
                        USING ERRCODE = '23514';
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM forecasts_forecastsnapshot AS source
                    JOIN forecasts_collectionjob AS job ON job.id = source.job_id
                    JOIN plants_plantconfiguration AS configuration
                      ON configuration.binding_id = source.binding_id
                    JOIN plants_calibrationrun AS calibration
                      ON calibration.id = NEW.calibration_id
                     AND calibration.configuration_id = configuration.id
                    WHERE source.id = NEW.snapshot_id
                      AND configuration.id = NEW.configuration_id
                      AND configuration.plant_id = NEW.plant_id
                      AND configuration.created_at <= source.collected_at
                      AND source.source_kind = 'forecast'
                      AND job.binding_id = source.binding_id
                      AND job.status = 'succeeded'
                      AND job.endpoint = 'forecasts'
                      AND job.reference_import_id IS NULL
                      AND source.collected_at < NEW.source_cutoff_at
                      AND source.available_at >= source.collected_at
                      AND source.available_at < NEW.source_cutoff_at
                      AND source.available_at <= NEW.reconstructed_at
                      AND (source.issued_at IS NULL OR source.issued_at <= source.collected_at)
                      AND NOT EXISTS (
                          SELECT 1 FROM forecasts_referencecapacitynormalization AS normalized
                          WHERE normalized.snapshot_id = source.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM jsonb_array_elements(source.normalized_intervals) AS row
                          WHERE COALESCE(row->>'source_kind', 'forecast') <> 'forecast'
                             OR COALESCE(row->'source_kinds', 'null'::jsonb)
                                NOT IN ('null'::jsonb, '[]'::jsonb)
                      )
                      AND calibration.plant_id = NEW.plant_id
                      AND calibration.binding_id = source.binding_id
                      AND calibration.factor = NEW.factor
                      AND calibration.status = 'ready'
                      AND calibration.created_at <= NEW.reconstructed_at
                      AND calibration.cutoff_at <= NEW.reconstructed_at
                      AND calibration.completed_at <= NEW.reconstructed_at
                      AND (
                          SELECT activation.calibration_id
                          FROM plants_calibrationactivation AS activation
                          WHERE activation.configuration_id = NEW.configuration_id
                            AND activation.created_at <= NEW.reconstructed_at
                          ORDER BY activation.created_at DESC, activation.id DESC LIMIT 1
                      ) = NEW.calibration_id
                ) THEN
                    RAISE EXCEPTION 'Forecast reconstruction lineage is invalid'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$;

            CREATE TRIGGER immutable_forecast_reconstruction
            BEFORE INSERT OR UPDATE ON plants_forecastreconstruction
            FOR EACH ROW EXECUTE FUNCTION guard_forecast_reconstruction();
            """,
            """
            DROP TRIGGER immutable_forecast_reconstruction ON plants_forecastreconstruction;
            DROP FUNCTION guard_forecast_reconstruction();
            ALTER TABLE plants_forecastreconstruction
                DROP CONSTRAINT reconstruction_calibration_configuration_fk;
            ALTER TABLE plants_forecastreconstruction
                DROP CONSTRAINT reconstruction_configuration_owner_fk;
            """,
        ),
    ]
