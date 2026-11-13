from typing import ClassVar

from django.db import migrations


class Migration(migrations.Migration):
    dependencies: ClassVar = [("plants", "0011_database_invariants")]

    operations: ClassVar = [
        migrations.RunSQL(
            (
                "ALTER TABLE plants_plant ADD CONSTRAINT plant_current_config_owner_fk "
                "FOREIGN KEY (current_configuration_id, id) REFERENCES "
                "plants_plantconfiguration (id, plant_id) DEFERRABLE INITIALLY DEFERRED NOT "
                "VALID"
            ),
            "ALTER TABLE plants_plant DROP CONSTRAINT plant_current_config_owner_fk",
        ),
        migrations.RunSQL(
            (
                "ALTER TABLE plants_productionobservation ADD CONSTRAINT "
                "observation_config_owner_fk FOREIGN KEY (configuration_id, plant_id) "
                "REFERENCES plants_plantconfiguration (id, plant_id) DEFERRABLE INITIALLY "
                "DEFERRED NOT VALID"
            ),
            "ALTER TABLE plants_productionobservation DROP CONSTRAINT observation_config_owner_fk",
        ),
        migrations.RunSQL(
            (
                "ALTER TABLE plants_calibrationrun ADD CONSTRAINT calibration_config_owner_fk "
                "FOREIGN KEY (configuration_id, plant_id) REFERENCES "
                "plants_plantconfiguration (id, plant_id) DEFERRABLE INITIALLY DEFERRED NOT "
                "VALID"
            ),
            "ALTER TABLE plants_calibrationrun DROP CONSTRAINT calibration_config_owner_fk",
        ),
        migrations.RunSQL(
            (
                "ALTER TABLE plants_calibrationrun ADD CONSTRAINT "
                "calibration_config_binding_fk FOREIGN KEY (configuration_id, binding_id) "
                "REFERENCES plants_plantconfiguration (id, binding_id) DEFERRABLE INITIALLY "
                "DEFERRED NOT VALID"
            ),
            "ALTER TABLE plants_calibrationrun DROP CONSTRAINT calibration_config_binding_fk",
        ),
        migrations.RunSQL(
            (
                "ALTER TABLE plants_calibrationactivation ADD CONSTRAINT "
                "activation_config_owner_fk FOREIGN KEY (configuration_id, plant_id) "
                "REFERENCES plants_plantconfiguration (id, plant_id) DEFERRABLE INITIALLY "
                "DEFERRED NOT VALID"
            ),
            "ALTER TABLE plants_calibrationactivation DROP CONSTRAINT activation_config_owner_fk",
        ),
        migrations.RunSQL(
            (
                "ALTER TABLE plants_calibrationactivation ADD CONSTRAINT "
                "activation_calibration_config_fk FOREIGN KEY (calibration_id, "
                "configuration_id) REFERENCES plants_calibrationrun (id, configuration_id) "
                "DEFERRABLE INITIALLY DEFERRED NOT VALID"
            ),
            (
                "ALTER TABLE plants_calibrationactivation DROP CONSTRAINT "
                "activation_calibration_config_fk"
            ),
        ),
        migrations.RunSQL(
            (
                "ALTER TABLE plants_calibrationrun ADD CONSTRAINT "
                "calibration_comparison_config_fk FOREIGN KEY (comparison_activation_id, "
                "configuration_id) REFERENCES plants_calibrationactivation (id, "
                "configuration_id) DEFERRABLE INITIALLY DEFERRED NOT VALID"
            ),
            "ALTER TABLE plants_calibrationrun DROP CONSTRAINT calibration_comparison_config_fk",
        ),
        migrations.RunSQL(
            (
                "ALTER TABLE plants_plantforecast ADD CONSTRAINT "
                "forecast_calibration_config_fk FOREIGN KEY (calibration_id, "
                "configuration_id) REFERENCES plants_calibrationrun (id, configuration_id) "
                "DEFERRABLE INITIALLY DEFERRED NOT VALID"
            ),
            "ALTER TABLE plants_plantforecast DROP CONSTRAINT forecast_calibration_config_fk",
        ),
    ]
