import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from django.conf import settings
from psycopg import sql

from deploy import bootstrap_database

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = (ROOT / "deploy/bootstrap.sql").read_text()
SALT = bytes(range(16))
CATALOG_SQL = """
SELECT 'role ' || rolname || ' ' || rolsuper || rolcreatedb || rolcreaterole
       || rolreplication || rolbypassrls || rolcanlogin || rolinherit
  FROM pg_roles WHERE rolname IN ('pyracast_owner', 'pyracast_app')
UNION ALL
SELECT 'database ' || coalesce(datacl::text, '') FROM pg_database
 WHERE datname = current_database()
UNION ALL
SELECT 'schema ' || nspowner::regrole || ' ' || coalesce(nspacl::text, '') FROM pg_namespace
 WHERE nspname = 'public'
UNION ALL
SELECT 'default ' || defaclrole::regrole || ' ' || defaclobjtype::text || ' ' || defaclacl::text
  FROM pg_default_acl
UNION ALL
SELECT 'extension ' || extname || ' ' || extnamespace::regnamespace FROM pg_extension
UNION ALL
SELECT 'table ' || relname || ' ' || relowner::regrole || ' ' || coalesce(relacl::text, '')
  FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'S')
ORDER BY 1
"""


def server_parameters(database: str) -> dict[str, Any]:
    default = settings.DATABASES["default"]
    return {
        "host": default["HOST"],
        "port": default["PORT"],
        "user": default["USER"],
        "password": default["PASSWORD"],
        "dbname": database,
    }


@pytest.fixture
def scratch_database() -> Iterator[str]:
    name = f"pyracast_bootstrap_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(**server_parameters("postgres"), autocommit=True) as server:
        server.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield name
    finally:
        with psycopg.connect(**server_parameters("postgres"), autocommit=True) as server:
            server.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def provision(database: str) -> list[str]:
    with psycopg.connect(**server_parameters(database)) as connection:
        # Roles belong to the cluster; concurrent test databases must not create them at once.
        connection.execute("SELECT pg_advisory_xact_lock(hashtext('pyracast-bootstrap'))")
        connection.execute(BOOTSTRAP.encode())
        return [row[0] for row in connection.execute(CATALOG_SQL)]


def as_owner(database: str, statement: str) -> None:
    with psycopg.connect(**server_parameters(database)) as connection:
        connection.execute("SET ROLE pyracast_owner")
        connection.execute(statement.encode())


def privileges(database: str, role: str, table: str) -> dict[str, bool]:
    with psycopg.connect(**server_parameters(database)) as connection:
        return {
            privilege: bool(
                connection.execute(
                    "SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)
                ).fetchone()
                == (True,)
            )
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
        }


def test_provisioning_converges_and_a_second_run_changes_nothing(scratch_database: str) -> None:
    provision(scratch_database)
    as_owner(
        scratch_database,
        "CREATE TABLE django_migrations (id bigserial PRIMARY KEY, app varchar(255), "
        "name varchar(255), applied timestamptz)",
    )
    as_owner(scratch_database, "CREATE TABLE accounts_probe (id bigserial PRIMARY KEY)")

    converged = provision(scratch_database)

    assert provision(scratch_database) == converged
    assert privileges(scratch_database, "pyracast_app", "django_migrations") == {
        "SELECT": True,
        "INSERT": False,
        "UPDATE": False,
        "DELETE": False,
        "TRUNCATE": False,
    }
    assert privileges(scratch_database, "pyracast_app", "accounts_probe") == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": True,
        "DELETE": True,
        "TRUNCATE": False,
    }
    with psycopg.connect(**server_parameters(scratch_database)) as connection:
        assert connection.execute(
            "SELECT has_database_privilege('pyracast_owner', current_database(), 'CREATE'), "
            "has_database_privilege('pyracast_app', current_database(), 'CONNECT'), "
            "has_schema_privilege('pyracast_app', 'public', 'CREATE'), "
            "(SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'public'), "
            "EXISTS (SELECT FROM pg_extension WHERE extname = 'btree_gist')"
        ).fetchone() == (False, True, False, "pyracast_owner", True)
        assert (
            connection.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolcanlogin FROM pg_roles "
                "WHERE rolname IN ('pyracast_owner', 'pyracast_app')"
            ).fetchall()
            == [(False, False, False, True)] * 2
        )


def test_scram_verifier_matches_the_one_postgresql_computes() -> None:
    role = f"pyracast_scram_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(**server_parameters("postgres")) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} PASSWORD 'stored-by-server'").format(sql.Identifier(role))
        )
        (stored,) = connection.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname = %s", (role,)
        ).fetchone() or (None,)
        verifier = bootstrap_database.scram_verifier("computed-by-client", SALT)
        connection.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(verifier))
        )
        (replaced,) = connection.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname = %s", (role,)
        ).fetchone() or (None,)
        connection.rollback()

    assert bootstrap_database.scram_matches(stored, "stored-by-server")
    assert not bootstrap_database.scram_matches(stored, "another-password")
    assert replaced == verifier
    assert bootstrap_database.scram_matches(replaced, "computed-by-client")


@pytest.mark.parametrize("stored", [None, "", "md5abc", "SCRAM-SHA-256$x", "SCRAM-SHA-256$1:%$a:b"])
def test_an_unreadable_verifier_never_matches(stored: str | None) -> None:
    assert not bootstrap_database.scram_matches(stored, "password")


def test_password_statements_set_only_the_roles_whose_verifier_differs() -> None:
    owner = bootstrap_database.scram_verifier("owner-password", SALT)
    stored = {"pyracast_owner": owner, "pyracast_app": "SCRAM-SHA-256$4096:AAAA$AAAA:AAAA"}
    passwords = {"pyracast_owner": "owner-password", "pyracast_app": "new-app-password"}

    statements = bootstrap_database.password_statements(stored, passwords, salt=lambda: SALT)

    assert statements == [
        "ALTER ROLE pyracast_app PASSWORD "
        f"'{bootstrap_database.scram_verifier('new-app-password', SALT)}';"
    ]
    assert (
        bootstrap_database.password_statements(
            {"pyracast_owner": owner, "pyracast_app": stored["pyracast_app"]},
            {"pyracast_owner": "owner-password"},
        )
        == []
    )


def test_password_statements_refuse_a_password_postgresql_would_normalize() -> None:
    with pytest.raises(ValueError, match="printable ASCII") as error:
        bootstrap_database.password_statements({}, {"pyracast_app": "paßword"})
    assert "paßword" not in str(error.value)


class Psql:
    def __init__(self, stored: str) -> None:
        self.stored = stored
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(
        self, command: list[str], *, input: str | None = None, **options: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, input))
        return subprocess.CompletedProcess(command, 0, stdout=self.stored if input is None else "")


def environment(directory: Path, app: str = "pyracast_app", owner: str = "pyracast_owner") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "runtime.env").write_text(f"PGUSER={app}\nPGPASSWORD=app-secret\n")
    (directory / "migration.env").write_text(f"PGUSER={owner}\nPGPASSWORD=owner-secret\n")


def release(root: Path) -> Path:
    (root / "deploy").mkdir(parents=True)
    (root / "deploy/bootstrap.sql").write_text(BOOTSTRAP)
    return root


def test_converge_applies_the_definition_in_one_transaction_without_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment(tmp_path / "secrets")
    psql = Psql("pyracast_owner\npyracast_app\n")
    monkeypatch.setattr(bootstrap_database.subprocess, "run", psql)

    bootstrap_database.converge_database(release(tmp_path / "release"), tmp_path / "secrets")

    (query, _), (apply, script) = psql.calls
    assert "pg_authid" in query[-1]
    assert "--single-transaction" in apply
    assert script is not None
    assert script.startswith(BOOTSTRAP)
    assert script.count("ALTER ROLE pyracast_owner PASSWORD 'SCRAM-SHA-256$4096:") == 1
    assert script.count("ALTER ROLE pyracast_app PASSWORD 'SCRAM-SHA-256$4096:") == 1
    assert "secret" not in script.replace("SECRET", "")
    assert not any("secret" in argument for command, _ in psql.calls for argument in command)


def test_converge_changes_no_password_that_already_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment(tmp_path / "secrets")
    stored = "".join(
        f"{role} {bootstrap_database.scram_verifier(password, SALT)}\n"
        for role, password in (("pyracast_owner", "owner-secret"), ("pyracast_app", "app-secret"))
    )
    psql = Psql(stored)
    monkeypatch.setattr(bootstrap_database.subprocess, "run", psql)

    bootstrap_database.converge_database(release(tmp_path / "release"), tmp_path / "secrets")

    assert psql.calls[-1][1] == BOOTSTRAP


@pytest.mark.parametrize(("app", "owner"), [("postgres", "pyracast_owner"), ("pyracast_app", "x")])
def test_converge_refuses_role_names_outside_the_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app: str, owner: str
) -> None:
    environment(tmp_path / "secrets", app, owner)
    psql = Psql("")
    monkeypatch.setattr(bootstrap_database.subprocess, "run", psql)

    with pytest.raises(ValueError, match="role names"):
        bootstrap_database.converge_database(release(tmp_path / "release"), tmp_path / "secrets")
    assert psql.calls == []


def test_bootstrap_is_one_idempotent_definition_without_its_own_transaction() -> None:
    statements = [line.strip().upper() for line in BOOTSTRAP.splitlines()]

    assert "BEGIN;" not in statements
    assert "COMMIT;" not in statements
    assert not any(line.startswith("CREATE ROLE ") for line in BOOTSTRAP.splitlines())
    assert "DATABASE pyracast" not in BOOTSTRAP
