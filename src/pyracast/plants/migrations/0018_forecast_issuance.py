from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies: ClassVar = [
        ("forecasts", "0019_repair_unused_quota_wait_attempts"),
        ("plants", "0017_production_observation_revisions"),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="ForecastIssuance",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("target_date", models.DateField()),
                ("published_at", models.DateTimeField()),
                ("cutoff_at", models.DateTimeField()),
                ("model_version", models.CharField(max_length=100)),
                ("factor", models.FloatField()),
                ("payloads", models.JSONField()),
                (
                    "outage_scenarios",
                    models.JSONField(),
                ),
                ("native_intervals", models.JSONField()),
                (
                    "calibration",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="plants.calibrationrun",
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
                    models.Index(fields=["plant", "target_date"], name="issuance_plant_day")
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("plant", "snapshot", "target_date"), name="forecast_issuance_unique"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("factor__gt", 0), ("factor__lt", float("inf"))),
                        name="forecast_issuance_factor_finite",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("published_at__lt", models.F("cutoff_at"))),
                        name="forecast_issuance_before_daylight",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("calibration__isnull", False), ("factor", 1), _connector="OR"
                        ),
                        name="issuance_baseline_factor_identity",
                    ),
                ],
            },
        ),
        migrations.RunSQL(
            """
            ALTER TABLE plants_forecastissuance
            ADD CONSTRAINT issuance_configuration_owner_fk
            FOREIGN KEY (configuration_id, plant_id)
            REFERENCES plants_plantconfiguration (id, plant_id)
            DEFERRABLE INITIALLY DEFERRED;

            ALTER TABLE plants_forecastissuance
            ADD CONSTRAINT issuance_calibration_configuration_fk
            FOREIGN KEY (calibration_id, configuration_id)
            REFERENCES plants_calibrationrun (id, configuration_id)
            DEFERRABLE INITIALLY DEFERRED;

            CREATE FUNCTION guard_forecast_issuance() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'UPDATE' THEN
                    RAISE EXCEPTION 'Published forecast issuances are immutable'
                        USING ERRCODE = '23514';
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM forecasts_forecastsnapshot AS source
                    JOIN plants_plantconfiguration AS configuration
                      ON configuration.binding_id = source.binding_id
                    WHERE source.id = NEW.snapshot_id
                      AND configuration.id = NEW.configuration_id
                      AND configuration.plant_id = NEW.plant_id
                      AND configuration.created_at <= source.collected_at
                      AND source.source_kind = 'forecast'
                      AND source.collected_at <= NEW.published_at
                      AND source.collected_at < NEW.cutoff_at
                      AND (source.issued_at IS NULL OR source.issued_at <= source.collected_at)
                      AND (
                        NEW.calibration_id IS NULL OR EXISTS (
                            SELECT 1
                            FROM plants_calibrationrun AS calibration
                            JOIN plants_calibrationactivation AS activation
                              ON activation.calibration_id = calibration.id
                             AND activation.configuration_id = NEW.configuration_id
                            WHERE calibration.id = NEW.calibration_id
                              AND calibration.configuration_id = NEW.configuration_id
                              AND calibration.factor = NEW.factor
                              AND calibration.status = 'ready'
                              AND calibration.created_at <= source.collected_at
                              AND calibration.cutoff_at <= source.collected_at
                              AND calibration.completed_at <= source.collected_at
                              AND activation.created_at <= source.collected_at
                        )
                      )
                ) THEN
                    RAISE EXCEPTION 'Forecast issuance acquisition lineage is invalid'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$;

            CREATE TRIGGER immutable_forecast_issuance
            BEFORE INSERT OR UPDATE ON plants_forecastissuance
            FOR EACH ROW EXECUTE FUNCTION guard_forecast_issuance();
            """,
            """
            DROP TRIGGER immutable_forecast_issuance ON plants_forecastissuance;
            DROP FUNCTION guard_forecast_issuance();
            ALTER TABLE plants_forecastissuance
                DROP CONSTRAINT issuance_calibration_configuration_fk;
            ALTER TABLE plants_forecastissuance
                DROP CONSTRAINT issuance_configuration_owner_fk;
            """,
        ),
    ]
