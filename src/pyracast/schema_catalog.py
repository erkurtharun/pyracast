import json
from collections.abc import Mapping
from pathlib import Path

from django.db.backends.base.base import BaseDatabaseWrapper
from psycopg import sql

type Catalog = dict[str, list[list[str]]]

# The leading values that name a row in its category; the others describe it.
NAME_WIDTH = {
    "tables": 1,
    "columns": 2,
    "constraints": 2,
    "indexes": 2,
    "sequences": 1,
    "triggers": 2,
    "functions": 2,
    "extensions": 1,
    "comments": 1,
    "unsupported": 1,
}
# bootstrap.sql installs it for the operator; no migration creates it and the application never
# reads it.
PROVISIONED_EXTENSIONS = ("pg_stat_statements",)
# PostgreSQL's FirstNormalObjectId: lower object ids belong to the cluster itself.
FIRST_CREATED_OID = 16384
SNAPSHOT = Path(__file__).with_name("schema_catalog.json")

MEMBERS = "member AS (SELECT classid::regclass AS kind, objid FROM pg_depend WHERE deptype = 'e')"
CATEGORIES = (
    """SELECT 'tables', ARRAY[c.relname::text, c.relpersistence::text,
        coalesce(array_to_string(c.reloptions, ','), ''), c.relrowsecurity::text,
        c.relforcerowsecurity::text, c.relreplident::text, am.amname::text,
        coalesce(ts.spcname::text, '')]
    FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
    JOIN pg_am AS am ON am.oid = c.relam
    LEFT JOIN pg_tablespace AS ts ON ts.oid = c.reltablespace
    WHERE n.nspname = current_schema() AND c.relkind = 'r'
        AND ('pg_class'::regclass, c.oid) NOT IN (TABLE member)""",
    """SELECT 'columns', ARRAY[c.relname::text, a.attname::text,
        (rank() OVER (PARTITION BY c.oid ORDER BY a.attnum))::text,
        format_type(a.atttypid, a.atttypmod), a.attnotnull::text,
        coalesce(pg_get_expr(ad.adbin, ad.adrelid), ''), a.attidentity::text,
        a.attgenerated::text, coalesce(co.collname::text, ''), a.attstorage::text,
        a.attcompression::text]
    FROM pg_attribute AS a JOIN pg_class AS c ON c.oid = a.attrelid
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef AS ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
    LEFT JOIN pg_collation AS co ON co.oid = a.attcollation
    WHERE n.nspname = current_schema() AND c.relkind = 'r' AND a.attnum > 0
        AND NOT a.attisdropped AND ('pg_class'::regclass, c.oid) NOT IN (TABLE member)""",
    """SELECT 'constraints', ARRAY[t.relname::text, k.conname::text, k.contype::text,
        pg_get_constraintdef(k.oid), k.condeferrable::text, k.condeferred::text,
        k.convalidated::text, k.connoinherit::text, k.conenforced::text,
        coalesce((SELECT string_agg(g.tgenabled::text, '' ORDER BY g.tgenabled::text)
            FROM pg_trigger AS g WHERE g.tgconstraint = k.oid), '')]
    FROM pg_constraint AS k JOIN pg_class AS t ON t.oid = k.conrelid
    JOIN pg_namespace AS n ON n.oid = t.relnamespace
    WHERE n.nspname = current_schema() AND ('pg_class'::regclass, t.oid) NOT IN (TABLE member)""",
    """SELECT 'indexes', ARRAY[t.relname::text, i.relname::text, pg_get_indexdef(i.oid),
        x.indisunique::text, x.indisprimary::text, x.indisexclusion::text,
        x.indimmediate::text, x.indisvalid::text, x.indnullsnotdistinct::text,
        x.indisclustered::text, x.indisreplident::text,
        coalesce(array_to_string(i.reloptions, ','), '')]
    FROM pg_index AS x JOIN pg_class AS i ON i.oid = x.indexrelid
    JOIN pg_class AS t ON t.oid = x.indrelid JOIN pg_namespace AS n ON n.oid = t.relnamespace
    WHERE n.nspname = current_schema() AND t.relkind = 'r'
        AND ('pg_class'::regclass, t.oid) NOT IN (TABLE member)""",
    """SELECT 'sequences', ARRAY[c.relname::text, format_type(s.seqtypid, NULL),
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
    WHERE n.nspname = current_schema() AND ('pg_class'::regclass, c.oid) NOT IN (TABLE member)""",
    """SELECT 'triggers', ARRAY[c.relname::text, g.tgname::text, pg_get_triggerdef(g.oid),
        g.tgenabled::text]
    FROM pg_trigger AS g JOIN pg_class AS c ON c.oid = g.tgrelid
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = current_schema() AND NOT g.tgisinternal
        AND ('pg_class'::regclass, c.oid) NOT IN (TABLE member)""",
    """SELECT 'functions', ARRAY[p.proname::text, pg_get_function_identity_arguments(p.oid),
        p.prokind::text, pg_get_functiondef(p.oid)]
    FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
    WHERE n.nspname = current_schema() AND p.prokind IN ('f', 'p')
        AND ('pg_proc'::regclass, p.oid) NOT IN (TABLE member)""",
    """SELECT 'extensions', ARRAY[e.extname::text, e.extversion, n.nspname::text]
    FROM pg_extension AS e JOIN pg_namespace AS n ON n.oid = e.extnamespace
    WHERE e.extname <> ALL({provisioned})""",
    """SELECT 'comments', ARRAY[pg_describe_object(d.classoid, d.objoid, d.objsubid),
        d.description]
    FROM pg_description AS d
    WHERE d.objoid >= {first_created} AND d.classoid <> 'pg_extension'::regclass
        AND (d.classoid::regclass, d.objoid) NOT IN (TABLE member)""",
)
IN_SCHEMA = "n.nspname = current_schema()"
# (catalog, rows of it, condition): objects of kinds the application never creates.
UNSUPPORTED = (
    (
        "pg_class",
        "pg_class AS item JOIN pg_namespace AS n ON n.oid = item.relnamespace",
        f"{IN_SCHEMA} AND item.relkind NOT IN ('r', 'i', 'S')",
    ),
    (
        "pg_type",
        "pg_type AS item JOIN pg_namespace AS n ON n.oid = item.typnamespace",
        f"{IN_SCHEMA} AND item.typrelid = 0"
        " AND NOT EXISTS (SELECT FROM pg_type AS element WHERE element.typarray = item.oid)",
    ),
    (
        "pg_namespace",
        "pg_namespace AS item",
        "item.nspname <> current_schema()"
        " AND item.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
        r" AND item.nspname !~ '^pg_(toast_)?temp_\d+$'",
    ),
    (
        "pg_policy",
        "pg_policy AS item JOIN pg_class AS c ON c.oid = item.polrelid"
        " JOIN pg_namespace AS n ON n.oid = c.relnamespace",
        IN_SCHEMA,
    ),
    (
        "pg_rewrite",
        "pg_rewrite AS item JOIN pg_class AS c ON c.oid = item.ev_class"
        " JOIN pg_namespace AS n ON n.oid = c.relnamespace",
        f"{IN_SCHEMA} AND ('pg_class'::regclass, c.oid) NOT IN (TABLE member)",
    ),
    (
        "pg_class",
        "pg_inherits AS inheritance JOIN pg_class AS item ON item.oid = inheritance.inhrelid"
        " JOIN pg_namespace AS n ON n.oid = item.relnamespace",
        IN_SCHEMA,
    ),
    (
        "pg_proc",
        "pg_proc AS item JOIN pg_namespace AS n ON n.oid = item.pronamespace",
        f"{IN_SCHEMA} AND item.prokind NOT IN ('f', 'p')",
    ),
    *(
        (catalog, f"{catalog} AS item JOIN pg_namespace AS n ON n.oid = item.{column}", IN_SCHEMA)
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
    ),
    *(
        (catalog, f"{catalog} AS item", f"item.oid >= {FIRST_CREATED_OID}")
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
    ),
    (
        "pg_subscription",
        "pg_subscription AS item",
        "item.subdbid = (SELECT oid FROM pg_database WHERE datname = current_database())",
    ),
)
UNSUPPORTED_ROWS = sql.SQL(
    "SELECT 'unsupported', ARRAY[pg_describe_object({catalog}::regclass, item.oid, 0)]"
    " FROM {rows} WHERE {condition} AND ({catalog}::regclass, item.oid) NOT IN (TABLE member)"
)
# One statement, so every category is read from one snapshot.
QUERY = sql.SQL("WITH {members} {selects}").format(
    members=sql.SQL(MEMBERS),
    selects=sql.SQL(" UNION ALL ").join(
        [
            *(
                sql.SQL(category).format(
                    provisioned=sql.Literal(list(PROVISIONED_EXTENSIONS)),
                    first_created=sql.Literal(FIRST_CREATED_OID),
                )
                for category in CATEGORIES
            ),
            *(
                UNSUPPORTED_ROWS.format(
                    catalog=sql.Literal(catalog), rows=sql.SQL(rows), condition=sql.SQL(condition)
                )
                for catalog, rows, condition in UNSUPPORTED
            ),
        ]
    ),
)


def read_catalog(connection: BaseDatabaseWrapper) -> Catalog:
    catalog: Catalog = {category: [] for category in NAME_WIDTH}
    with connection.cursor() as cursor:
        cursor.execute(QUERY)
        for category, row in cursor.fetchall():
            catalog[category].append(row)
    for rows in catalog.values():
        rows.sort()
    return catalog


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
