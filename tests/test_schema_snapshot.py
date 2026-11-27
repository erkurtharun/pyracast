import json
import os
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from _pytest.mark import ParameterSet
from django.db import connection, transaction
from psycopg import sql

from pyracast import schema_catalog
from pyracast.schema_catalog import (
    READS,
    SNAPSHOT,
    differences,
    read_catalog,
    recorded,
    serialized,
    statement,
)
from scripts import write_schema_catalog

pytestmark = pytest.mark.django_db

GUARD = "productionobservation_revision_guard"
GUARD_FUNCTION = "guard_productionobservation_revision"
INDEX = "regions_region_deleted_at_8f56ef2c"
KEY = "region_approved_binding_fk"
NAME_TYPE = "character varying(200)"
FOREIGN_KEY_TRIGGERS = f"constraint triggers regions_region.{KEY}"

type Statement = str | sql.Composable


def change(
    name: str,
    statements: Sequence[Statement],
    named: Sequence[str],
    prepared: Sequence[Statement] = (),
) -> ParameterSet:
    return pytest.param(list(prepared), list(statements), list(named), id=name)


def unsupported(name: str, statements: Sequence[Statement], *named: str) -> ParameterSet:
    return change(name, statements, [f"unsupported {found}: unexpected" for found in named])


def disabled_key_trigger(table: str, function: str) -> Statement:
    return sql.SQL(
        "DO $$ BEGIN EXECUTE format('ALTER TABLE %I DISABLE TRIGGER %I', {table},"
        " (SELECT g.tgname FROM pg_trigger AS g JOIN pg_constraint AS k ON k.oid = g.tgconstraint"
        " JOIN pg_proc AS p ON p.oid = g.tgfoid WHERE k.conname = {key} AND p.proname = {function}"
        ")); END $$"
    ).format(table=sql.Literal(table), key=sql.Literal(KEY), function=sql.Literal(function))


CHANGES = [
    change(
        "default",
        ["ALTER TABLE regions_region ALTER COLUMN name SET DEFAULT 'Site'"],
        ["columns regions_region.name: changed"],
    ),
    change(
        "type",
        ["ALTER TABLE regions_region ALTER COLUMN deleted_at TYPE timestamp"],
        ["columns regions_region.deleted_at: changed"],
    ),
    change(
        "moved-column",
        [
            "ALTER TABLE regions_region DROP COLUMN purge_warning_sent_at",
            "ALTER TABLE regions_region ADD COLUMN purge_warning_sent_at timestamp with time zone",
        ],
        [
            "columns regions_region.purge_bindings: changed",
            "columns regions_region.purge_plant_ids: changed",
            "columns regions_region.purge_warning_sent_at: changed",
        ],
    ),
    change(
        "array-dimensions",
        ["ALTER TABLE regions_region ALTER COLUMN purge_plant_ids TYPE bigint[][]"],
        ["columns regions_region.purge_plant_ids: changed"],
    ),
    change(
        "statistics-target",
        ["ALTER TABLE regions_region ALTER COLUMN name SET STATISTICS 500"],
        ["columns regions_region.name: changed"],
    ),
    change(
        "collation-schema",
        [f'ALTER TABLE regions_region ALTER COLUMN name TYPE {NAME_TYPE} COLLATE extra_schema."C"'],
        ["columns regions_region.name: changed"],
        prepared=[
            "CREATE SCHEMA extra_schema",
            'CREATE COLLATION extra_schema."C" FROM pg_catalog."C"',
            f'ALTER TABLE regions_region ALTER COLUMN name TYPE {NAME_TYPE} COLLATE pg_catalog."C"',
        ],
    ),
    change(
        "not-null",
        ["ALTER TABLE regions_region ALTER COLUMN deleted_at SET NOT NULL"],
        [
            "columns regions_region.deleted_at: changed",
            "constraints regions_region.regions_region_deleted_at_not_null: unexpected",
        ],
    ),
    change(
        "renamed-constraint",
        ["ALTER TABLE regions_region RENAME CONSTRAINT region_status_chk TO region_state_chk"],
        [
            "constraints regions_region.region_state_chk: unexpected",
            "constraints regions_region.region_status_chk: missing",
        ],
    ),
    change(
        "constraint-definition",
        [
            "ALTER TABLE regions_region DROP CONSTRAINT region_latitude_chk,"
            " ADD CONSTRAINT region_latitude_chk CHECK (latitude BETWEEN -89 AND 89)",
        ],
        ["constraints regions_region.region_latitude_chk: changed"],
    ),
    change(
        "constraint-timing",
        [f"ALTER TABLE regions_region ALTER CONSTRAINT {KEY} NOT DEFERRABLE"],
        [f"constraints regions_region.{KEY}: changed"],
    ),
    change(
        "key-insert-check",
        [disabled_key_trigger("regions_region", "RI_FKey_check_ins")],
        [f"{FOREIGN_KEY_TRIGGERS}.regions_region.INSERT.RI_FKey_check_ins: changed"],
    ),
    change(
        "key-delete-check",
        [disabled_key_trigger("connections_sitebinding", "RI_FKey_noaction_del")],
        [f"{FOREIGN_KEY_TRIGGERS}.connections_sitebinding.DELETE.RI_FKey_noaction_del: changed"],
    ),
    change(
        "missing-index",
        [f"DROP INDEX {INDEX}"],
        [f"indexes regions_region.{INDEX}: missing"],
    ),
    change(
        "index-definition",
        [f"DROP INDEX {INDEX}", f"CREATE INDEX {INDEX} ON regions_region (deleted_at DESC)"],
        [f"indexes regions_region.{INDEX}: changed"],
    ),
    change(
        "index-storage",
        [f"ALTER INDEX {INDEX} SET (fillfactor = 50)"],
        [f"indexes regions_region.{INDEX}: changed"],
    ),
    change(
        "sequence",
        ["ALTER SEQUENCE regions_region_id_seq INCREMENT BY 2"],
        ["sequences regions_region_id_seq: changed"],
    ),
    change(
        "extra-sequence",
        ["CREATE SEQUENCE regions_extra_seq"],
        ["sequences regions_extra_seq: unexpected"],
    ),
    change(
        "disabled-trigger",
        [f"ALTER TABLE plants_productionobservation DISABLE TRIGGER {GUARD}"],
        [f"triggers plants_productionobservation.{GUARD}: changed"],
    ),
    change(
        "missing-trigger",
        [f"DROP TRIGGER {GUARD} ON plants_productionobservation"],
        [f"triggers plants_productionobservation.{GUARD}: missing"],
    ),
    change(
        "function-body",
        [
            f"CREATE OR REPLACE FUNCTION {GUARD_FUNCTION}() RETURNS trigger LANGUAGE plpgsql"
            " AS $$BEGIN RETURN NEW; END$$"
        ],
        [f"functions {GUARD_FUNCTION}.: changed"],
    ),
    change(
        "extra-function",
        ["CREATE FUNCTION extra_function() RETURNS integer LANGUAGE sql AS 'SELECT 1'"],
        ["functions extra_function.: unexpected"],
    ),
    change(
        "extra-table",
        ["CREATE TABLE extra_table (id integer)"],
        ["columns extra_table.id: unexpected", "tables extra_table: unexpected"],
    ),
    change(
        "table-storage",
        ["ALTER TABLE regions_region SET (fillfactor = 50)"],
        ["tables regions_region: changed"],
    ),
    change(
        "toast-storage",
        ["ALTER TABLE forecasts_forecastsnapshot SET (toast.autovacuum_enabled = false)"],
        ["tables forecasts_forecastsnapshot: changed"],
    ),
    change(
        "storage-option-order",
        [
            "ALTER TABLE regions_region RESET (fillfactor, autovacuum_enabled)",
            "ALTER TABLE regions_region SET (autovacuum_enabled = false, fillfactor = 50)",
        ],
        [],
        prepared=["ALTER TABLE regions_region SET (fillfactor = 50, autovacuum_enabled = false)"],
    ),
    change(
        "row-security",
        ["ALTER TABLE regions_region ENABLE ROW LEVEL SECURITY"],
        ["tables regions_region: changed"],
    ),
    change(
        "extension",
        ["CREATE EXTENSION pg_trgm"],
        ["comments extension pg_trgm: unexpected", "extensions pg_trgm: unexpected"],
    ),
    change(
        "column-comment",
        ["COMMENT ON COLUMN regions_region.name IS 'Site name'"],
        ["comments column name of table regions_region: unexpected"],
    ),
    change(
        "schema-comment",
        ["COMMENT ON SCHEMA public IS 'Pyracast'"],
        ["comments schema public: changed"],
    ),
    change(
        "extension-comment",
        ["COMMENT ON EXTENSION btree_gist IS 'Exclusion constraints'"],
        ["comments extension btree_gist: changed"],
    ),
    unsupported(
        "view",
        ["CREATE VIEW extra_view AS SELECT 1 AS one"],
        "pg_class view extra_view",
        "pg_rewrite rule _RETURN on view extra_view",
    ),
    unsupported(
        "materialized-view",
        ["CREATE MATERIALIZED VIEW extra_view AS SELECT 1 AS one"],
        "pg_class materialized view extra_view",
        "pg_rewrite rule _RETURN on materialized view extra_view",
    ),
    unsupported(
        "composite-type",
        ["CREATE TYPE extra_pair AS (one integer)"],
        "pg_class composite type extra_pair",
    ),
    unsupported(
        "foreign-table",
        [
            "CREATE FOREIGN DATA WRAPPER extra_wrapper",
            "CREATE SERVER extra_server FOREIGN DATA WRAPPER extra_wrapper",
            "CREATE FOREIGN TABLE extra_foreign (id integer) SERVER extra_server",
        ],
        "pg_class foreign table extra_foreign",
        "pg_foreign_data_wrapper foreign-data wrapper extra_wrapper",
        "pg_foreign_server server extra_server",
    ),
    change(
        "partitioned-table",
        ["CREATE TABLE extra_parted (id integer) PARTITION BY RANGE (id)"],
        ["unsupported pg_class table extra_parted: unexpected"],
    ),
    change(
        "inheritance",
        [
            "CREATE TABLE extra_parent (id integer)",
            "CREATE TABLE extra_child () INHERITS (extra_parent)",
        ],
        [
            "columns extra_child.id: unexpected",
            "columns extra_parent.id: unexpected",
            "tables extra_child: unexpected",
            "tables extra_parent: unexpected",
            "unsupported pg_inherits table extra_child: unexpected",
        ],
    ),
    unsupported("enum", ["CREATE TYPE extra_kind AS ENUM ('one')"], "pg_type type extra_kind"),
    unsupported(
        "domain",
        ["CREATE DOMAIN extra_positive AS integer CHECK (VALUE > 0)"],
        "pg_type type extra_positive",
    ),
    change(
        "range-type",
        ["CREATE TYPE extra_range AS RANGE (SUBTYPE = integer)"],
        [
            "functions extra_multirange.: unexpected",
            "functions extra_multirange.VARIADIC extra_range[]: unexpected",
            "functions extra_multirange.extra_range: unexpected",
            "functions extra_range.integer, integer: unexpected",
            "functions extra_range.integer, integer, text: unexpected",
            "unsupported pg_cast cast from extra_range to extra_multirange: unexpected",
            "unsupported pg_type type extra_multirange: unexpected",
            "unsupported pg_type type extra_range: unexpected",
        ],
    ),
    unsupported("shell-type", ["CREATE TYPE extra_shell"], "pg_type type extra_shell"),
    unsupported("schema", ["CREATE SCHEMA extra_schema"], "pg_namespace schema extra_schema"),
    change(
        "object-in-another-schema",
        ["CREATE TABLE information_schema.extra_table (id integer)"],
        [
            "unsupported pg_class table information_schema.extra_table: unexpected",
            "unsupported pg_type type information_schema.extra_table: unexpected",
            "unsupported pg_type type information_schema.extra_table[]: unexpected",
        ],
    ),
    unsupported(
        "policy",
        ["CREATE POLICY extra_policy ON regions_region USING (true)"],
        "pg_policy policy extra_policy on table regions_region",
    ),
    unsupported(
        "rule",
        ["CREATE RULE extra_rule AS ON DELETE TO regions_region DO INSTEAD NOTHING"],
        "pg_rewrite rule extra_rule on table regions_region",
    ),
    unsupported(
        "statistics",
        ["CREATE STATISTICS extra_statistics ON latitude, longitude FROM regions_region"],
        "pg_statistic_ext statistics object extra_statistics",
    ),
    unsupported(
        "aggregate",
        ["CREATE AGGREGATE extra_sum (integer) (SFUNC = int4pl, STYPE = integer)"],
        "pg_proc function extra_sum(integer)",
    ),
    unsupported(
        "operator",
        ["CREATE OPERATOR === (LEFTARG = integer, RIGHTARG = integer, FUNCTION = int4eq)"],
        "pg_operator operator ===(integer,integer)",
    ),
    unsupported(
        "operator-class",
        [
            "CREATE OPERATOR CLASS extra_class FOR TYPE integer USING btree"
            " AS OPERATOR 1 <, FUNCTION 1 btint4cmp(integer, integer)"
        ],
        "pg_opclass operator class extra_class for access method btree",
        "pg_opfamily operator family extra_class for access method btree",
    ),
    unsupported(
        "operator-family",
        ["CREATE OPERATOR FAMILY extra_family USING btree"],
        "pg_opfamily operator family extra_family for access method btree",
    ),
    unsupported(
        "collation",
        ['CREATE COLLATION extra_collation FROM "C"'],
        "pg_collation collation extra_collation",
    ),
    unsupported(
        "conversion",
        ["CREATE CONVERSION extra_conversion FOR 'UTF8' TO 'LATIN1' FROM utf8_to_iso8859_1"],
        "pg_conversion conversion extra_conversion",
    ),
    unsupported(
        "text-search-configuration",
        ["CREATE TEXT SEARCH CONFIGURATION extra_configuration (COPY = english)"],
        "pg_ts_config text search configuration extra_configuration",
    ),
    unsupported(
        "text-search-dictionary",
        ["CREATE TEXT SEARCH DICTIONARY extra_dictionary (TEMPLATE = simple)"],
        "pg_ts_dict text search dictionary extra_dictionary",
    ),
    unsupported(
        "text-search-parser",
        [
            "CREATE TEXT SEARCH PARSER extra_parser (START = prsd_start,"
            " GETTOKEN = prsd_nexttoken, END = prsd_end, LEXTYPES = prsd_lextype)"
        ],
        "pg_ts_parser text search parser extra_parser",
    ),
    unsupported(
        "text-search-template",
        [
            "CREATE TEXT SEARCH TEMPLATE extra_template"
            " (LEXIZE = dsimple_lexize, INIT = dsimple_init)"
        ],
        "pg_ts_template text search template extra_template",
    ),
    unsupported(
        "cast",
        [
            "CREATE TYPE extra_kind AS ENUM ('one')",
            "CREATE CAST (extra_kind AS integer) WITH INOUT",
        ],
        "pg_cast cast from extra_kind to integer",
        "pg_type type extra_kind",
    ),
    unsupported(
        "language",
        ["CREATE LANGUAGE extra_language HANDLER plpgsql_call_handler"],
        "pg_language language extra_language",
    ),
    unsupported(
        "access-method",
        ["CREATE ACCESS METHOD extra_method TYPE TABLE HANDLER heap_tableam_handler"],
        "pg_am access method extra_method",
    ),
    unsupported(
        "transform",
        [
            "CREATE TRANSFORM FOR tsvector LANGUAGE plpgsql"
            " (FROM SQL WITH FUNCTION gtsvector_compress(internal),"
            " TO SQL WITH FUNCTION tsvectorrecv(internal))"
        ],
        "pg_transform transform for tsvector language plpgsql",
    ),
    change(
        "event-trigger",
        [
            "CREATE FUNCTION extra_event() RETURNS event_trigger LANGUAGE plpgsql AS $$BEGIN END$$",
            "CREATE EVENT TRIGGER extra_event ON ddl_command_start EXECUTE FUNCTION extra_event()",
        ],
        [
            "functions extra_event.: unexpected",
            "unsupported pg_event_trigger event trigger extra_event: unexpected",
        ],
    ),
    unsupported(
        "publication",
        ["CREATE PUBLICATION extra_publication"],
        "pg_publication publication extra_publication",
    ),
    unsupported(
        "subscription",
        [
            "CREATE SUBSCRIPTION extra_subscription CONNECTION 'dbname=none'"
            " PUBLICATION extra_publication WITH (connect = false)"
        ],
        "pg_subscription subscription extra_subscription",
    ),
    unsupported(
        "large-object",
        ["SELECT lo_create(424242)"],
        "pg_largeobject_metadata large object 424242",
    ),
]


def executed(statements: Sequence[Statement]) -> None:
    with connection.cursor() as cursor:
        for executed_statement in statements:
            cursor.execute(executed_statement)


def test_the_migrated_schema_is_recorded_byte_for_byte_in_the_snapshot() -> None:
    catalog = read_catalog(connection)

    assert differences(catalog, recorded()) == []
    assert serialized(catalog).encode("utf-8") == SNAPSHOT.read_bytes()


@pytest.mark.parametrize(("prepared", "statements", "named"), CHANGES)
def test_the_comparison_names_each_kind_of_change(
    prepared: list[Statement], statements: list[Statement], named: list[str]
) -> None:
    original = read_catalog(connection)
    with transaction.atomic():
        executed(prepared)
        before = read_catalog(connection)
        executed(statements)
        found = differences(read_catalog(connection), before)
        transaction.set_rollback(True)

    assert found == named
    assert read_catalog(connection) == original


@pytest.fixture
def spare_tablespace() -> Iterator[str]:
    name = f"spare_{os.getpid()}"
    admin = connection.get_new_connection(connection.get_connection_params())
    admin.autocommit = True
    try:
        # A tablespace cannot be created inside the case's transaction.
        admin.execute("SET allow_in_place_tablespaces = on")
        admin.execute(sql.SQL("CREATE TABLESPACE {} LOCATION ''").format(sql.Identifier(name)))
        try:
            yield name
        finally:
            admin.execute(sql.SQL("DROP TABLESPACE {}").format(sql.Identifier(name)))
    finally:
        admin.close()


@pytest.mark.parametrize(
    ("moved", "named"),
    [
        (f"ALTER INDEX {INDEX}", f"indexes regions_region.{INDEX}: changed"),
        ("ALTER TABLE regions_region", "tables regions_region: changed"),
    ],
    ids=["index", "table"],
)
def test_the_comparison_names_a_relation_moved_to_another_tablespace(
    moved: str, named: str, spare_tablespace: str
) -> None:
    before = read_catalog(connection)
    with transaction.atomic():
        executed([f"{moved} SET TABLESPACE {spare_tablespace}"])
        found = differences(read_catalog(connection), before)
        transaction.set_rollback(True)

    assert found == [named]


@pytest.mark.parametrize("name", READS)
def test_a_read_left_out_of_the_statement_is_named_as_not_read(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = {other: read for other, read in READS.items() if other != name}
    monkeypatch.setattr(schema_catalog, "QUERY", statement(kept))

    assert differences(read_catalog(connection), recorded()) == [f"{name}: not read"]


def test_a_read_the_snapshot_does_not_record_is_named_as_not_expected() -> None:
    expected = {name: rows for name, rows in recorded().items() if name != "functions"}

    assert differences(read_catalog(connection), expected) == ["functions: not expected"]


def test_the_writer_records_the_catalog_of_the_database_it_is_connected_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = tmp_path / "schema_catalog.json"
    monkeypatch.setattr(write_schema_catalog, "SNAPSHOT", written)
    with transaction.atomic():
        executed(["COMMENT ON TABLE regions_region IS 'Recorded'"])
        catalog = read_catalog(connection)
        write_schema_catalog.write()
        transaction.set_rollback(True)

    assert written.read_bytes() == serialized(catalog).encode("utf-8")
    assert ["table regions_region", "Recorded"] in json.loads(written.read_bytes())["comments"]
