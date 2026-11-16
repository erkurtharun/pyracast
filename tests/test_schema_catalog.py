import pytest
from django.apps import apps
from django.db import connection, models

pytestmark = pytest.mark.django_db(transaction=True)


def test_native_foreign_keys_have_validated_deferred_ownership_targets() -> None:
    expected = {
        "approved_binding_region_fk": (
            "regions_region",
            ("approved_binding_id", "id"),
            "connections_sitebinding",
            ("id", "region_id"),
        ),
        "request_binding_account_fk": (
            "forecasts_requestattempt",
            ("binding_id", "provider_account_id"),
            "connections_sitebinding",
            ("id", "provider_account_id"),
        ),
        "request_job_binding_fk": (
            "forecasts_requestattempt",
            ("job_id", "binding_id"),
            "forecasts_collectionjob",
            ("id", "binding_id"),
        ),
        "collection_import_binding_fk": (
            "forecasts_collectionjob",
            ("reference_import_id", "binding_id"),
            "forecasts_historicalreferenceimport",
            ("id", "binding_id"),
        ),
        "snapshot_job_binding_fk": (
            "forecasts_forecastsnapshot",
            ("job_id", "binding_id"),
            "forecasts_collectionjob",
            ("id", "binding_id"),
        ),
        "normalization_snapshot_binding_fk": (
            "forecasts_referencecapacitynormalization",
            ("snapshot_id", "target_binding_id"),
            "forecasts_forecastsnapshot",
            ("id", "binding_id"),
        ),
        "plant_current_config_owner_fk": (
            "plants_plant",
            ("current_configuration_id", "id"),
            "plants_plantconfiguration",
            ("id", "plant_id"),
        ),
        "observation_config_owner_fk": (
            "plants_productionobservation",
            ("configuration_id", "plant_id"),
            "plants_plantconfiguration",
            ("id", "plant_id"),
        ),
        "calibration_config_owner_fk": (
            "plants_calibrationrun",
            ("configuration_id", "plant_id"),
            "plants_plantconfiguration",
            ("id", "plant_id"),
        ),
        "calibration_config_binding_fk": (
            "plants_calibrationrun",
            ("configuration_id", "binding_id"),
            "plants_plantconfiguration",
            ("id", "binding_id"),
        ),
        "activation_config_owner_fk": (
            "plants_calibrationactivation",
            ("configuration_id", "plant_id"),
            "plants_plantconfiguration",
            ("id", "plant_id"),
        ),
        "activation_calibration_config_fk": (
            "plants_calibrationactivation",
            ("calibration_id", "configuration_id"),
            "plants_calibrationrun",
            ("id", "configuration_id"),
        ),
        "calibration_comparison_config_fk": (
            "plants_calibrationrun",
            ("comparison_activation_id", "configuration_id"),
            "plants_calibrationactivation",
            ("id", "configuration_id"),
        ),
        "forecast_calibration_config_fk": (
            "plants_plantforecast",
            ("calibration_id", "configuration_id"),
            "plants_calibrationrun",
            ("id", "configuration_id"),
        ),
        "incident_declared_config_owner_fk": (
            "incidents_plantincident",
            ("declared_configuration_id", "plant_id"),
            "plants_plantconfiguration",
            ("id", "plant_id"),
        ),
        "epias_snapshot_job_lineage_fk": (
            "metering_epiassnapshot",
            ("job_id", "connection_id", "configuration_id"),
            "metering_epiascollectionjob",
            ("id", "connection_id", "configuration_id"),
        ),
        "configuration_plant_region_fk": (
            "plants_plantconfiguration",
            ("plant_id", "region_id"),
            "plants_plant",
            ("id", "region_id"),
        ),
        "configuration_binding_region_fk": (
            "plants_plantconfiguration",
            ("binding_id", "region_id"),
            "connections_sitebinding",
            ("id", "region_id"),
        ),
        "epias_job_connection_plant_fk": (
            "metering_epiascollectionjob",
            ("connection_id", "plant_id"),
            "metering_epiasconnection",
            ("id", "plant_id"),
        ),
        "epias_job_configuration_plant_fk": (
            "metering_epiascollectionjob",
            ("configuration_id", "plant_id"),
            "plants_plantconfiguration",
            ("id", "plant_id"),
        ),
        "proposal_configuration_owner_fk": (
            "change_requests_changerequest",
            ("configuration_id", "plant_id"),
            "plants_plantconfiguration",
            ("id", "plant_id"),
        ),
    }
    project_models = tuple(
        model
        for model in apps.get_models(include_auto_created=True)
        if model.__module__.startswith("pyracast.")
    )
    project_tables = sorted({model._meta.db_table for model in project_models})
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.conname, child.relname,
                ARRAY(
                    SELECT a.attname::text
                    FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, position)
                    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                    ORDER BY k.position
                ), parent.relname,
                ARRAY(
                    SELECT a.attname::text
                    FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, position)
                    JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum
                    ORDER BY k.position
                ), c.convalidated, c.condeferrable, c.condeferred
            FROM pg_constraint c
            JOIN pg_class child ON child.oid = c.conrelid
            JOIN pg_namespace ns ON ns.oid = child.relnamespace
            JOIN pg_class parent ON parent.oid = c.confrelid
            WHERE c.contype = 'f' AND ns.nspname = current_schema()
                AND child.relname::text = ANY(%s)
            """,
            (project_tables,),
        )
        rows = cursor.fetchall()
    actual = {
        name: (child, tuple(columns), parent, tuple(targets))
        for name, child, columns, parent, targets, *_ in rows
    }
    assert expected.keys() <= actual.keys()
    for name, definition in expected.items():
        assert actual[name] == definition, name
    model_foreign_keys = {
        (
            model._meta.db_table,
            (field.column,),
            field.target_field.model._meta.db_table,
            (field.target_field.column,),
        )
        for model in project_models
        for field in model._meta.local_fields
        if isinstance(field, models.ForeignKey)
        and field.deconstruct()[3].get("db_constraint", True)
    }
    assert model_foreign_keys <= set(actual.values())
    assert all(
        validated and deferred and initially_deferred
        for *_, validated, deferred, initially_deferred in rows
    )
