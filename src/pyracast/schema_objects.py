from dataclasses import dataclass

from django.contrib.postgres.operations import CreateExtension
from django.db.migrations.operations import RunSQL
from django.db.migrations.operations.base import Operation

type MigrationKey = tuple[str, str]


@dataclass(frozen=True)
class Extension:
    name: str
    migration: MigrationKey


@dataclass(frozen=True)
class CompositeForeignKey:
    name: str
    table: str
    columns: tuple[str, ...]
    target: str
    target_columns: tuple[str, ...]
    migration: MigrationKey

    def create_sql(self) -> list[str]:
        return [
            f"ALTER TABLE {self.table} ADD CONSTRAINT {self.name} "
            f"FOREIGN KEY ({', '.join(self.columns)}) "
            f"REFERENCES {self.target} ({', '.join(self.target_columns)}) "
            "DEFERRABLE INITIALLY DEFERRED"
        ]

    def drop_sql(self) -> list[str]:
        return [f"ALTER TABLE {self.table} DROP CONSTRAINT {self.name}"]


@dataclass(frozen=True)
class RowTrigger:
    name: str
    table: str
    timing: str
    function: str
    body: str
    migration: MigrationKey

    def create_sql(self) -> list[str]:
        return [
            f"CREATE FUNCTION {self.function}() RETURNS trigger LANGUAGE plpgsql "
            f"AS $${self.body}$$",
            f"CREATE TRIGGER {self.name} {self.timing} ON {self.table} "
            f"FOR EACH ROW EXECUTE FUNCTION {self.function}()",
        ]

    def drop_sql(self) -> list[str]:
        return [f"DROP TRIGGER {self.name} ON {self.table}", f"DROP FUNCTION {self.function}()"]


type SchemaObject = Extension | CompositeForeignKey | RowTrigger


def _owner_key(
    name: str,
    table: str,
    columns: tuple[str, ...],
    target: str,
    target_columns: tuple[str, ...],
    app_label: str,
    migration: str = "0001_initial",
) -> CompositeForeignKey:
    return CompositeForeignKey(name, table, columns, target, target_columns, (app_label, migration))


# The body is compared verbatim with pg_proc.prosrc, whitespace included. It restates the
# publication lineage rule of plants/forecast_archive.py; DELETE stays unguarded for the purge.
_ISSUANCE_GUARD = """
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
            """

# Raw database objects that Django models cannot declare. Each names the migration that
# creates it; an object added later belongs to the new migration that creates it, never to an
# applied one. The catalog test compares this list with the built schema.
SCHEMA_OBJECTS: tuple[SchemaObject, ...] = (
    # Needed by the GiST exclusion constraints of plants and incidents. Production provisions it
    # administratively (deploy/bootstrap.sql); the migration then skips the statement.
    Extension("btree_gist", ("accounts", "0001_initial")),
    _owner_key(
        "approved_binding_region_fk",
        "regions_region",
        ("approved_binding_id", "id"),
        "connections_sitebinding",
        ("id", "region_id"),
        "connections",
        "0002_initial",
    ),
    _owner_key(
        "request_binding_account_fk",
        "forecasts_requestattempt",
        ("binding_id", "provider_account_id"),
        "connections_sitebinding",
        ("id", "provider_account_id"),
        "forecasts",
    ),
    _owner_key(
        "request_job_binding_fk",
        "forecasts_requestattempt",
        ("job_id", "binding_id"),
        "forecasts_collectionjob",
        ("id", "binding_id"),
        "forecasts",
    ),
    _owner_key(
        "collection_import_binding_fk",
        "forecasts_collectionjob",
        ("reference_import_id", "binding_id"),
        "forecasts_historicalreferenceimport",
        ("id", "binding_id"),
        "forecasts",
    ),
    _owner_key(
        "snapshot_job_binding_fk",
        "forecasts_forecastsnapshot",
        ("job_id", "binding_id"),
        "forecasts_collectionjob",
        ("id", "binding_id"),
        "forecasts",
    ),
    _owner_key(
        "normalization_snapshot_binding_fk",
        "forecasts_referencecapacitynormalization",
        ("snapshot_id", "target_binding_id"),
        "forecasts_forecastsnapshot",
        ("id", "binding_id"),
        "forecasts",
    ),
    _owner_key(
        "plant_current_config_owner_fk",
        "plants_plant",
        ("current_configuration_id", "id"),
        "plants_plantconfiguration",
        ("id", "plant_id"),
        "plants",
    ),
    _owner_key(
        "configuration_plant_region_fk",
        "plants_plantconfiguration",
        ("plant_id", "region_id"),
        "plants_plant",
        ("id", "region_id"),
        "plants",
    ),
    _owner_key(
        "configuration_binding_region_fk",
        "plants_plantconfiguration",
        ("binding_id", "region_id"),
        "connections_sitebinding",
        ("id", "region_id"),
        "plants",
    ),
    _owner_key(
        "observation_config_owner_fk",
        "plants_productionobservation",
        ("configuration_id", "plant_id"),
        "plants_plantconfiguration",
        ("id", "plant_id"),
        "plants",
    ),
    _owner_key(
        "calibration_config_owner_fk",
        "plants_calibrationrun",
        ("configuration_id", "plant_id"),
        "plants_plantconfiguration",
        ("id", "plant_id"),
        "plants",
    ),
    _owner_key(
        "calibration_config_binding_fk",
        "plants_calibrationrun",
        ("configuration_id", "binding_id"),
        "plants_plantconfiguration",
        ("id", "binding_id"),
        "plants",
    ),
    _owner_key(
        "activation_config_owner_fk",
        "plants_calibrationactivation",
        ("configuration_id", "plant_id"),
        "plants_plantconfiguration",
        ("id", "plant_id"),
        "plants",
    ),
    _owner_key(
        "activation_calibration_config_fk",
        "plants_calibrationactivation",
        ("calibration_id", "configuration_id"),
        "plants_calibrationrun",
        ("id", "configuration_id"),
        "plants",
    ),
    _owner_key(
        "calibration_comparison_config_fk",
        "plants_calibrationrun",
        ("comparison_activation_id", "configuration_id"),
        "plants_calibrationactivation",
        ("id", "configuration_id"),
        "plants",
    ),
    _owner_key(
        "issuance_configuration_owner_fk",
        "plants_forecastissuance",
        ("configuration_id", "plant_id"),
        "plants_plantconfiguration",
        ("id", "plant_id"),
        "plants",
    ),
    _owner_key(
        "issuance_calibration_configuration_fk",
        "plants_forecastissuance",
        ("calibration_id", "configuration_id"),
        "plants_calibrationrun",
        ("id", "configuration_id"),
        "plants",
    ),
    RowTrigger(
        "immutable_forecast_issuance",
        "plants_forecastissuance",
        "BEFORE INSERT OR UPDATE",
        "guard_forecast_issuance",
        _ISSUANCE_GUARD,
        ("plants", "0001_initial"),
    ),
    _owner_key(
        "incident_declared_config_owner_fk",
        "incidents_plantincident",
        ("declared_configuration_id", "plant_id"),
        "plants_plantconfiguration",
        ("id", "plant_id"),
        "incidents",
    ),
    _owner_key(
        "epias_job_connection_plant_fk",
        "metering_epiascollectionjob",
        ("connection_id", "plant_id"),
        "metering_epiasconnection",
        ("id", "plant_id"),
        "metering",
    ),
    _owner_key(
        "epias_job_configuration_plant_fk",
        "metering_epiascollectionjob",
        ("configuration_id", "plant_id"),
        "plants_plantconfiguration",
        ("id", "plant_id"),
        "metering",
    ),
    _owner_key(
        "epias_snapshot_job_lineage_fk",
        "metering_epiassnapshot",
        ("job_id", "connection_id", "configuration_id"),
        "metering_epiascollectionjob",
        ("id", "connection_id", "configuration_id"),
        "metering",
    ),
    _owner_key(
        "proposal_configuration_owner_fk",
        "change_requests_changerequest",
        ("configuration_id", "plant_id"),
        "plants_plantconfiguration",
        ("id", "plant_id"),
        "change_requests",
    ),
)


def operations(app_label: str, migration: str) -> list[Operation]:
    selected = [item for item in SCHEMA_OBJECTS if item.migration == (app_label, migration)]
    created: list[Operation] = [
        CreateExtension(item.name) for item in selected if isinstance(item, Extension)
    ]
    raw = [item for item in selected if not isinstance(item, Extension)]
    if raw:
        created.append(
            RunSQL(
                [sql for item in raw for sql in item.create_sql()],
                [sql for item in reversed(raw) for sql in item.drop_sql()],
            )
        )
    return created
