import pytest
from django.db import connection, transaction

from pyracast.schema_catalog import SNAPSHOT, differences, read_catalog, recorded, serialized

pytestmark = pytest.mark.django_db

GUARD = "productionobservation_revision_guard"
GUARD_FUNCTION = "guard_productionobservation_revision"
CHANGES = [
    pytest.param(
        ["ALTER TABLE regions_region ALTER COLUMN name SET DEFAULT 'Site'"],
        ["columns regions_region.name: changed"],
        id="default",
    ),
    pytest.param(
        ["ALTER TABLE regions_region ALTER COLUMN deleted_at TYPE timestamp"],
        ["columns regions_region.deleted_at: changed"],
        id="type",
    ),
    pytest.param(
        [
            "ALTER TABLE regions_region DROP COLUMN purge_warning_sent_at",
            "ALTER TABLE regions_region ADD COLUMN purge_warning_sent_at timestamp with time zone",
        ],
        [
            "columns regions_region.purge_bindings: changed",
            "columns regions_region.purge_plant_ids: changed",
            "columns regions_region.purge_warning_sent_at: changed",
        ],
        id="moved-column",
    ),
    pytest.param(
        ["ALTER TABLE regions_region ALTER COLUMN deleted_at SET NOT NULL"],
        [
            "columns regions_region.deleted_at: changed",
            "constraints regions_region.regions_region_deleted_at_not_null: unexpected",
        ],
        id="not-null",
    ),
    pytest.param(
        [
            "ALTER TABLE regions_region RENAME CONSTRAINT region_status_chk TO region_state_chk",
        ],
        [
            "constraints regions_region.region_state_chk: unexpected",
            "constraints regions_region.region_status_chk: missing",
        ],
        id="renamed-constraint",
    ),
    pytest.param(
        [
            "ALTER TABLE regions_region DROP CONSTRAINT region_latitude_chk,"
            " ADD CONSTRAINT region_latitude_chk CHECK (latitude BETWEEN -89 AND 89)",
        ],
        ["constraints regions_region.region_latitude_chk: changed"],
        id="constraint-definition",
    ),
    pytest.param(
        ["ALTER TABLE regions_region ALTER CONSTRAINT region_approved_binding_fk NOT DEFERRABLE"],
        ["constraints regions_region.region_approved_binding_fk: changed"],
        id="constraint-timing",
    ),
    pytest.param(
        [
            "DO $$ BEGIN EXECUTE format('ALTER TABLE regions_region DISABLE TRIGGER %I',"
            " (SELECT min(tgname) FROM pg_trigger WHERE tgrelid = 'regions_region'::regclass"
            " AND tgconstraint = (SELECT oid FROM pg_constraint"
            " WHERE conname = 'region_approved_binding_fk'))); END $$"
        ],
        ["constraints regions_region.region_approved_binding_fk: changed"],
        id="foreign-key-trigger",
    ),
    pytest.param(
        ["DROP INDEX regions_region_deleted_at_8f56ef2c"],
        ["indexes regions_region.regions_region_deleted_at_8f56ef2c: missing"],
        id="missing-index",
    ),
    pytest.param(
        [
            "DROP INDEX regions_region_deleted_at_8f56ef2c",
            "CREATE INDEX regions_region_deleted_at_8f56ef2c ON regions_region (deleted_at DESC)",
        ],
        ["indexes regions_region.regions_region_deleted_at_8f56ef2c: changed"],
        id="index-definition",
    ),
    pytest.param(
        ["ALTER SEQUENCE regions_region_id_seq INCREMENT BY 2"],
        ["sequences regions_region_id_seq: changed"],
        id="sequence",
    ),
    pytest.param(
        ["CREATE SEQUENCE regions_extra_seq"],
        ["sequences regions_extra_seq: unexpected"],
        id="extra-sequence",
    ),
    pytest.param(
        [f"ALTER TABLE plants_productionobservation DISABLE TRIGGER {GUARD}"],
        [f"triggers plants_productionobservation.{GUARD}: changed"],
        id="disabled-trigger",
    ),
    pytest.param(
        [f"DROP TRIGGER {GUARD} ON plants_productionobservation"],
        [f"triggers plants_productionobservation.{GUARD}: missing"],
        id="missing-trigger",
    ),
    pytest.param(
        [
            f"CREATE OR REPLACE FUNCTION {GUARD_FUNCTION}() RETURNS trigger LANGUAGE plpgsql"
            " AS $$BEGIN RETURN NEW; END$$"
        ],
        [f"functions {GUARD_FUNCTION}.: changed"],
        id="function-body",
    ),
    pytest.param(
        ["CREATE FUNCTION extra_function() RETURNS integer LANGUAGE sql AS 'SELECT 1'"],
        ["functions extra_function.: unexpected"],
        id="extra-function",
    ),
    pytest.param(
        ["CREATE TABLE extra_table (id integer)"],
        ["tables extra_table: unexpected", "columns extra_table.id: unexpected"],
        id="extra-table",
    ),
    pytest.param(
        ["ALTER TABLE regions_region SET (fillfactor = 50)"],
        ["tables regions_region: changed"],
        id="table-storage",
    ),
    pytest.param(
        ["ALTER TABLE regions_region ENABLE ROW LEVEL SECURITY"],
        ["tables regions_region: changed"],
        id="row-security",
    ),
    pytest.param(
        ["CREATE EXTENSION pg_trgm"],
        ["extensions pg_trgm: unexpected"],
        id="extension",
    ),
    pytest.param(
        ["COMMENT ON COLUMN regions_region.name IS 'Site name'"],
        ["comments column name of table regions_region: unexpected"],
        id="comment",
    ),
    pytest.param(
        ["CREATE VIEW extra_view AS SELECT 1 AS one"],
        [
            "unsupported rule _RETURN on view extra_view: unexpected",
            "unsupported view extra_view: unexpected",
        ],
        id="view",
    ),
    pytest.param(
        ["CREATE TYPE extra_kind AS ENUM ('one')"],
        ["unsupported type extra_kind: unexpected"],
        id="enum",
    ),
    pytest.param(
        ["CREATE DOMAIN extra_positive AS integer CHECK (VALUE > 0)"],
        ["unsupported type extra_positive: unexpected"],
        id="domain",
    ),
    pytest.param(
        ["CREATE SCHEMA extra_schema"],
        ["unsupported schema extra_schema: unexpected"],
        id="schema",
    ),
    pytest.param(
        ["CREATE POLICY extra_policy ON regions_region USING (true)"],
        ["unsupported policy extra_policy on table regions_region: unexpected"],
        id="policy",
    ),
    pytest.param(
        ["CREATE RULE extra_rule AS ON DELETE TO regions_region DO INSTEAD NOTHING"],
        ["unsupported rule extra_rule on table regions_region: unexpected"],
        id="rule",
    ),
    pytest.param(
        ["CREATE STATISTICS extra_statistics ON latitude, longitude FROM regions_region"],
        ["unsupported statistics object extra_statistics: unexpected"],
        id="statistics",
    ),
    pytest.param(
        ["CREATE AGGREGATE extra_sum (integer) (SFUNC = int4pl, STYPE = integer)"],
        ["unsupported function extra_sum(integer): unexpected"],
        id="aggregate",
    ),
    pytest.param(
        ["CREATE PUBLICATION extra_publication"],
        ["unsupported publication extra_publication: unexpected"],
        id="publication",
    ),
]


def test_the_migrated_schema_is_recorded_byte_for_byte_in_the_snapshot() -> None:
    catalog = read_catalog(connection)

    assert differences(catalog, recorded()) == []
    assert serialized(catalog) == SNAPSHOT.read_text(encoding="utf-8")


@pytest.mark.parametrize(("statements", "named"), CHANGES)
def test_the_comparison_names_each_kind_of_change(statements: list[str], named: list[str]) -> None:
    before = read_catalog(connection)
    with transaction.atomic():
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        found = differences(read_catalog(connection), before)
        transaction.set_rollback(True)

    assert found == named
    assert read_catalog(connection) == before


def test_a_category_that_was_not_read_or_is_not_known_is_a_difference() -> None:
    expected = recorded()
    found = {**{key: rows for key, rows in expected.items() if key != "functions"}, "views": []}

    assert differences(found, expected) == ["functions: not read", "views: not expected"]
