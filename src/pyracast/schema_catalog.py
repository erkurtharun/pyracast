import json
from collections.abc import Iterable, Mapping
from pathlib import Path

from django.db.backends.base.base import BaseDatabaseWrapper
from psycopg import sql

type Catalog = dict[str, list[list[str]]]

# The leading values that name a row in its category; the others describe it.
NAME_WIDTH = {
    "tables": 1,
    "columns": 2,
    "constraints": 2,
    "constraint triggers": 5,
    "indexes": 2,
    "sequences": 1,
    "triggers": 2,
    "functions": 2,
    "extensions": 1,
    "comments": 1,
}
# bootstrap.sql installs it for the operator; no migration creates it, the application ignores it.
PROVISIONED_EXTENSIONS = ("pg_stat_statements",)
# PostgreSQL's FirstNormalObjectId: lower object ids belong to the cluster itself.
FIRST_CREATED_OID = 16384
SNAPSHOT = Path(__file__).with_name("schema_catalog.json")

SORTED_OPTIONS = sql.SQL(
    "array_to_string(ARRAY(SELECT option FROM unnest({}.reloptions) AS option"
    " ORDER BY option COLLATE \"C\"), ',')"
)
PARTS = {
    "in_schema": sql.SQL("n.nspname = current_schema()"),
    "not_member": sql.SQL("('pg_class'::regclass, c.oid) NOT IN (TABLE member)"),
    "provisioned": sql.Literal(list(PROVISIONED_EXTENSIONS)),
    "table_options": SORTED_OPTIONS.format(sql.Identifier("c")),
    "toast_options": SORTED_OPTIONS.format(sql.Identifier("toast")),
    "index_options": SORTED_OPTIONS.format(sql.Identifier("i")),
    "first_created": sql.Literal(FIRST_CREATED_OID),
    "elsewhere": sql.SQL(
        "n.nspname <> current_schema() AND item.oid >= {}"
        r" AND n.nspname <> 'pg_toast' AND n.nspname !~ '^pg_(toast_)?temp_\d+$'"
    ).format(sql.Literal(FIRST_CREATED_OID)),
}


def _composed(text: str) -> sql.Composed:
    return sql.SQL(text).format(**PARTS)


COMMON = _composed("""member AS (
    SELECT classid::regclass AS kind, objid FROM pg_depend WHERE deptype = 'e'
    UNION SELECT part.classid::regclass, part.objid
    FROM pg_depend AS part JOIN member AS owner
        ON owner.kind = part.refclassid::regclass AND owner.objid = part.refobjid
    WHERE part.deptype = 'i'
),
compared AS (
    SELECT 'pg_namespace'::regclass AS kind, n.oid AS objid FROM pg_namespace AS n WHERE {in_schema}
    UNION ALL SELECT 'pg_class'::regclass, c.oid
    FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE {in_schema} AND c.relkind IN ('r', 'i', 'S')
    UNION ALL SELECT 'pg_constraint'::regclass, k.oid
    FROM pg_constraint AS k JOIN pg_namespace AS n ON n.oid = k.connamespace WHERE {in_schema}
    UNION ALL SELECT 'pg_trigger'::regclass, g.oid
    FROM pg_trigger AS g JOIN pg_class AS c ON c.oid = g.tgrelid
    JOIN pg_namespace AS n ON n.oid = c.relnamespace WHERE {in_schema}
    UNION ALL SELECT 'pg_proc'::regclass, p.oid
    FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
    WHERE {in_schema} AND p.prokind IN ('f', 'p')
    UNION ALL SELECT 'pg_extension'::regclass, e.oid FROM pg_extension AS e
    WHERE e.extname <> ALL({provisioned})
),
key_trigger AS (
    SELECT g.oid, g.tgenabled::text AS enabled, ARRAY[c.relname::text, k.conname::text,
        fired.relname::text,
        concat_ws(' OR ', CASE WHEN g.tgtype & 4 <> 0 THEN 'INSERT' END,
            CASE WHEN g.tgtype & 8 <> 0 THEN 'DELETE' END,
            CASE WHEN g.tgtype & 16 <> 0 THEN 'UPDATE' END,
            CASE WHEN g.tgtype & 32 <> 0 THEN 'TRUNCATE' END),
        enforcing.proname::text] AS identity
    FROM pg_trigger AS g JOIN pg_constraint AS k ON k.oid = g.tgconstraint
    JOIN pg_proc AS enforcing ON enforcing.oid = g.tgfoid
    JOIN pg_class AS c ON c.oid = k.conrelid JOIN pg_namespace AS n ON n.oid = c.relnamespace
    JOIN pg_class AS fired ON fired.oid = g.tgrelid
    WHERE {in_schema} AND g.tgisinternal AND {not_member}
)""")
CATEGORIES = {
    "tables": """SELECT ARRAY[c.relname::text, c.relpersistence::text, {table_options},
        {toast_options}, c.relrowsecurity::text, c.relforcerowsecurity::text,
        c.relreplident::text, am.amname::text, coalesce(ts.spcname::text, '')]
    FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
    JOIN pg_am AS am ON am.oid = c.relam
    LEFT JOIN pg_class AS toast ON toast.oid = c.reltoastrelid
    LEFT JOIN pg_tablespace AS ts ON ts.oid = c.reltablespace
    WHERE {in_schema} AND c.relkind = 'r' AND {not_member}""",
    "columns": """SELECT ARRAY[c.relname::text, a.attname::text,
        (rank() OVER (PARTITION BY c.oid ORDER BY a.attnum))::text,
        format_type(a.atttypid, a.atttypmod), a.attndims::text, a.attnotnull::text,
        coalesce(pg_get_expr(ad.adbin, ad.adrelid), ''), a.attidentity::text,
        a.attgenerated::text, coalesce(cn.nspname || '.' || co.collname, ''),
        coalesce(a.attstattarget::text, ''), a.attstorage::text, a.attcompression::text]
    FROM pg_attribute AS a JOIN pg_class AS c ON c.oid = a.attrelid
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef AS ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
    LEFT JOIN pg_collation AS co ON co.oid = a.attcollation
    LEFT JOIN pg_namespace AS cn ON cn.oid = co.collnamespace
    WHERE {in_schema} AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
        AND {not_member}""",
    "constraints": """SELECT ARRAY[c.relname::text, k.conname::text, k.contype::text,
        pg_get_constraintdef(k.oid), k.condeferrable::text, k.condeferred::text,
        k.convalidated::text, k.connoinherit::text, k.conenforced::text]
    FROM pg_constraint AS k JOIN pg_class AS c ON c.oid = k.conrelid
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE {in_schema} AND {not_member}""",
    "constraint triggers": "SELECT identity || enabled FROM key_trigger",
    "indexes": """SELECT ARRAY[c.relname::text, i.relname::text, pg_get_indexdef(i.oid),
        x.indisunique::text, x.indisprimary::text, x.indisexclusion::text,
        x.indimmediate::text, x.indisvalid::text, x.indnullsnotdistinct::text,
        x.indisclustered::text, x.indisreplident::text, {index_options},
        coalesce(ts.spcname::text, '')]
    FROM pg_index AS x JOIN pg_class AS i ON i.oid = x.indexrelid
    JOIN pg_class AS c ON c.oid = x.indrelid JOIN pg_namespace AS n ON n.oid = c.relnamespace
    LEFT JOIN pg_tablespace AS ts ON ts.oid = i.reltablespace
    WHERE {in_schema} AND c.relkind = 'r' AND {not_member}""",
    "sequences": """SELECT ARRAY[c.relname::text, format_type(s.seqtypid, NULL),
        s.seqstart::text, s.seqincrement::text, s.seqmin::text, s.seqmax::text,
        s.seqcache::text, s.seqcycle::text,
        coalesce((SELECT t.relname || '.' || a.attname || ' ' || d.deptype::text
            FROM pg_depend AS d JOIN pg_class AS t ON t.oid = d.refobjid
            JOIN pg_attribute AS a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
            WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')), ''),
        c.relpersistence::text]
    FROM pg_sequence AS s JOIN pg_class AS c ON c.oid = s.seqrelid
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE {in_schema} AND {not_member}""",
    "triggers": """SELECT ARRAY[c.relname::text, g.tgname::text, pg_get_triggerdef(g.oid),
        g.tgenabled::text]
    FROM pg_trigger AS g JOIN pg_class AS c ON c.oid = g.tgrelid
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE {in_schema} AND NOT g.tgisinternal AND {not_member}""",
    "functions": """SELECT ARRAY[p.proname::text, pg_get_function_identity_arguments(p.oid),
        p.prokind::text, pg_get_functiondef(p.oid)]
    FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
    WHERE {in_schema} AND p.prokind IN ('f', 'p')
        AND ('pg_proc'::regclass, p.oid) NOT IN (TABLE member)""",
    "extensions": """SELECT ARRAY[e.extname::text, e.extversion, n.nspname::text]
    FROM pg_extension AS e JOIN pg_namespace AS n ON n.oid = e.extnamespace
    WHERE e.extname <> ALL({provisioned})""",
    "comments": """SELECT ARRAY[
        coalesce('constraint trigger ' || array_to_string(keyed.identity, '.'),
            object.type || ' ' || object.identity),
        d.description]
    FROM pg_description AS d
    JOIN compared ON compared.kind = d.classoid::regclass AND compared.objid = d.objoid
    CROSS JOIN LATERAL pg_identify_object(d.classoid, d.objoid, d.objsubid) AS object
    LEFT JOIN key_trigger AS keyed
        ON d.classoid = 'pg_trigger'::regclass AND keyed.oid = d.objoid
    WHERE (compared.kind, compared.objid) NOT IN (TABLE member)""",
}
UNSUPPORTED_READ = sql.SQL(
    "SELECT ARRAY[pg_describe_object({described}::regclass, item.oid, 0)] FROM {rows}"
    " WHERE ({condition}) AND ({described}::regclass, item.oid) NOT IN (TABLE member)"
)
NAMESPACED = sql.SQL("{} AS item JOIN pg_namespace AS n ON n.oid = item.{}")


def _unsupported(described_as: str, rows: sql.Composable, condition: str) -> sql.Composed:
    return UNSUPPORTED_READ.format(
        described=sql.Literal(described_as), rows=rows, condition=_composed(condition)
    )


def _namespaced(catalog: str, column: str, kind_in_schema: str = "true") -> sql.Composed:
    rows = NAMESPACED.format(sql.Identifier(catalog), sql.Identifier(column))
    return _unsupported(catalog, rows, "({in_schema} AND " + kind_in_schema + ") OR ({elsewhere})")


def _of_relations_in_schema(catalog: str, relation: str) -> sql.Composed:
    rows = sql.SQL(
        "{} AS item JOIN pg_class AS c ON c.oid = item.{}"
        " JOIN pg_namespace AS n ON n.oid = c.relnamespace"
    ).format(sql.Identifier(catalog), sql.Identifier(relation))
    return _unsupported(catalog, rows, "{in_schema}")


def _created(catalog: str) -> sql.Composed:
    rows = sql.SQL("{} AS item").format(sql.Identifier(catalog))
    return _unsupported(catalog, rows, "item.oid >= {first_created}")


# Objects of kinds the application never creates, and any object of a schema other than its own.
UNSUPPORTED = {
    "pg_class": _namespaced("pg_class", "relnamespace", "item.relkind NOT IN ('r', 'i', 'S')"),
    "pg_type": _namespaced(
        "pg_type",
        "typnamespace",
        "item.typrelid = 0"
        " AND NOT EXISTS (SELECT FROM pg_type AS element WHERE element.typarray = item.oid)",
    ),
    "pg_proc": _namespaced("pg_proc", "pronamespace", "item.prokind NOT IN ('f', 'p')"),
    **{
        catalog: _namespaced(catalog, column)
        for catalog, column in (
            ("pg_statistic_ext", "stxnamespace"),
            ("pg_operator", "oprnamespace"),
            ("pg_opclass", "opcnamespace"),
            ("pg_opfamily", "opfnamespace"),
            ("pg_collation", "collnamespace"),
            ("pg_conversion", "connamespace"),
            ("pg_ts_config", "cfgnamespace"),
            ("pg_ts_dict", "dictnamespace"),
            ("pg_ts_parser", "prsnamespace"),
            ("pg_ts_template", "tmplnamespace"),
        )
    },
    "pg_namespace": _unsupported(
        "pg_namespace",
        sql.SQL("pg_namespace AS item"),
        "item.nspname <> current_schema()"
        " AND item.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
        r" AND item.nspname !~ '^pg_(toast_)?temp_\d+$'",
    ),
    "pg_policy": _of_relations_in_schema("pg_policy", "polrelid"),
    "pg_rewrite": _of_relations_in_schema("pg_rewrite", "ev_class"),
    "pg_inherits": _unsupported(
        "pg_class",
        sql.SQL(
            "pg_inherits AS inheritance JOIN pg_class AS item ON item.oid = inheritance.inhrelid"
            " JOIN pg_namespace AS n ON n.oid = item.relnamespace"
        ),
        "{in_schema}",
    ),
    **{
        catalog: _created(catalog)
        for catalog in (
            "pg_cast",
            "pg_foreign_data_wrapper",
            "pg_foreign_server",
            "pg_language",
            "pg_am",
            "pg_transform",
            "pg_event_trigger",
            "pg_publication",
        )
    },
    "pg_subscription": _unsupported(
        "pg_subscription",
        sql.SQL("pg_subscription AS item"),
        "item.subdbid = (SELECT oid FROM pg_database WHERE datname = current_database())",
    ),
    "pg_largeobject_metadata": _unsupported(
        "pg_largeobject", sql.SQL("pg_largeobject_metadata AS item"), "true"
    ),
}
READS: dict[str, sql.Composable] = {
    **{name: _composed(read) for name, read in CATEGORIES.items()},
    **{f"unsupported {catalog}": read for catalog, read in UNSUPPORTED.items()},
}
READ = sql.SQL(
    "SELECT {name}, (SELECT coalesce(array_agg(found.entry), ARRAY[]::text[])"
    " FROM ({read}) AS found(entry))"
)


def statement(reads: Mapping[str, sql.Composable]) -> sql.Composed:
    """One statement, so every read sees one snapshot; each read yields its row even when empty."""
    return sql.SQL("WITH RECURSIVE {common} {reads}").format(
        common=COMMON,
        reads=sql.SQL(" UNION ALL ").join(
            READ.format(name=sql.Literal(name), read=read) for name, read in reads.items()
        ),
    )


QUERY = statement(READS)


def catalog_of(reads: Iterable[tuple[str, list[list[str]]]]) -> Catalog:
    return {name: sorted(rows) for name, rows in sorted(reads)}


def read_catalog(connection: BaseDatabaseWrapper) -> Catalog:
    with connection.cursor() as cursor:
        cursor.execute(QUERY)
        return catalog_of(cursor.fetchall())


def _by_name(rows: list[list[str]], width: int) -> dict[str, list[list[str]]]:
    named: dict[str, list[list[str]]] = {}
    for row in rows:
        named.setdefault(".".join(row[:width]), []).append(row)
    return named


def _category_differences(
    category: str, found: list[list[str]], expected: list[list[str]]
) -> list[str]:
    width = NAME_WIDTH.get(category, 1)
    have, want = _by_name(found, width), _by_name(expected, width)
    named = []
    for name in sorted(have.keys() | want.keys()):
        if name not in have:
            named.append(f"{category} {name}: missing")
        elif name not in want:
            named.append(f"{category} {name}: unexpected")
        elif have[name] != want[name]:
            named.append(f"{category} {name}: changed")
    return named


def differences(
    found: Mapping[str, list[list[str]]], expected: Mapping[str, list[list[str]]]
) -> list[str]:
    named = [f"{category}: not read" for category in expected if category not in found]
    named += [f"{category}: not expected" for category in found if category not in expected]
    for category in expected:
        if category in found:
            named += _category_differences(category, found[category], expected[category])
    return named


def _listed(rows: list[list[str]]) -> str:
    if not rows:
        return "[]"
    return "[\n" + ",\n".join(f"  {json.dumps(row, ensure_ascii=False)}" for row in rows) + "\n ]"


def serialized(catalog: Catalog) -> str:
    listed = (f"{json.dumps(category)}: {_listed(rows)}" for category, rows in catalog.items())
    return "{\n " + ",\n ".join(listed) + "\n}\n"


def recorded() -> Catalog:
    catalog: Catalog = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return catalog
