import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from django.conf import settings
from psycopg import sql

from deploy import bootstrap_database, reset_database

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


def role_check(database: str) -> None:
    with psycopg.connect(**server_parameters(database), autocommit=True) as connection:
        connection.execute(reset_database.ROLE_CHECK_SQL.encode())


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


def test_role_check_refuses_runtime_writes_to_the_ledger_until_provisioning_converges(
    scratch_database: str,
) -> None:
    provision(scratch_database)
    as_owner(
        scratch_database,
        "CREATE TABLE django_migrations (id bigserial PRIMARY KEY, app varchar(255), "
        "name varchar(255), applied timestamptz)",
    )
    as_owner(scratch_database, "GRANT INSERT ON django_migrations TO pyracast_app")

    with pytest.raises(psycopg.errors.RaiseException, match="migration ledger"):
        role_check(scratch_database)
    provision(scratch_database)
    role_check(scratch_database)


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


@pytest.mark.parametrize(
    "password",
    [
        "owner password",
        " tab\tand trailing ",
        "control\x80",
        *(
            "".join(map(chr, points))
            for points in (
                (0x70, 0xDF),
                (0xFF46, 0xFF55, 0xFF4C),
                (0x61, 0xAD, 0x62),
                (0x61, 0xA0, 0x62),
                (0x627, 0x644, 0x639),
                (0x627, 0x61),
                (0x627, 0x31, 0x627),
                (0x61, 0x221),
                (0x200B,),
                (0x61, 0x200B),
                (0xAD,),
                (0x65, 0x301),
                (0x1100, 0x1161),
                (0x61, 0xE000),
                (0x61, 0x1F600),
            )
        ),
    ],
)
def test_scram_verifier_matches_postgresql_for_every_password_it_accepts(password: str) -> None:
    role = f"pyracast_scram_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(**server_parameters("postgres")) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        (stored,) = connection.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname = %s", (role,)
        ).fetchone() or (None,)
        connection.rollback()

    assert bootstrap_database.scram_matches(stored, password)
    statements = bootstrap_database.password_statements(
        {"pyracast_app": stored}, {"pyracast_app": password}
    )
    assert statements == []


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
    monkeypatch.setattr(subprocess, "run", psql)

    changed = bootstrap_database.converge_database(
        release(tmp_path / "release"), tmp_path / "secrets"
    )

    assert changed == ["pyracast_owner"]
    (query, _), (apply, script) = psql.calls
    assert "pg_authid" in query[-1]
    assert "--single-transaction" in apply
    assert script is not None
    assert script.startswith(BOOTSTRAP)
    assert script.count("ALTER ROLE pyracast_owner PASSWORD 'SCRAM-SHA-256$4096:") == 1
    assert "ALTER ROLE pyracast_app PASSWORD" not in script
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
    monkeypatch.setattr(subprocess, "run", psql)

    changed = bootstrap_database.converge_database(
        release(tmp_path / "release"), tmp_path / "secrets"
    )

    assert changed == []
    assert psql.calls[-1][1] == BOOTSTRAP


@pytest.mark.parametrize(("app", "owner"), [("postgres", "pyracast_owner"), ("pyracast_app", "x")])
def test_converge_refuses_role_names_outside_the_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app: str, owner: str
) -> None:
    environment(tmp_path / "secrets", app, owner)
    psql = Psql("")
    monkeypatch.setattr(subprocess, "run", psql)

    with pytest.raises(ValueError, match="role names"):
        bootstrap_database.converge_database(release(tmp_path / "release"), tmp_path / "secrets")
    assert psql.calls == []


def test_bootstrap_is_one_idempotent_definition_without_its_own_transaction() -> None:
    statements = [line.strip().upper() for line in BOOTSTRAP.splitlines()]

    assert "BEGIN;" not in statements
    assert "COMMIT;" not in statements
    assert not any(line.startswith("CREATE ROLE ") for line in BOOTSTRAP.splitlines())
    assert "DATABASE pyracast" not in BOOTSTRAP


class ServerPsql:
    def __init__(self, database: str) -> None:
        self.database = database
        self.scripts: list[str] = []

    def __call__(
        self, command: list[str], *, input: str | None = None, **options: Any
    ) -> subprocess.CompletedProcess[str]:
        with psycopg.connect(**server_parameters(self.database)) as connection:
            if input is None:
                rows = connection.execute(command[-1].encode()).fetchall()
                stdout = "".join(f"{row[0]}\n" for row in rows)
                return subprocess.CompletedProcess(command, 0, stdout=stdout)
            self.scripts.append(input)
            connection.execute(input.encode())
        return subprocess.CompletedProcess(command, 0, stdout="")


def runtime_verifier(database: str) -> str | None:
    with psycopg.connect(**server_parameters(database)) as connection:
        (verifier,) = connection.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname = 'pyracast_app'"
        ).fetchone() or (None,)
    return verifier


def set_runtime_verifier(database: str, verifier: str | None) -> None:
    with psycopg.connect(**server_parameters(database), autocommit=True) as connection:
        connection.execute(
            sql.SQL("ALTER ROLE pyracast_app PASSWORD {}").format(sql.Literal(verifier))
        )


@pytest.fixture
def runtime_role(scratch_database: str) -> Iterator[str]:
    with psycopg.connect(**server_parameters(scratch_database)) as connection:
        # The role belongs to the cluster: hold its password still for the other test databases.
        connection.execute("SELECT pg_advisory_lock(hashtext('pyracast-runtime-role'))")
        provision(scratch_database)
        original = runtime_verifier(scratch_database)
        try:
            yield scratch_database
        finally:
            set_runtime_verifier(scratch_database, original)
            connection.execute("SELECT pg_advisory_unlock(hashtext('pyracast-runtime-role'))")


def test_the_runtime_password_switches_once_and_reverts_to_the_identical_verifier(
    runtime_role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = bootstrap_database.scram_verifier("previous-runtime-password", SALT)
    set_runtime_verifier(runtime_role, previous)
    environment(tmp_path / "secrets")
    saved = tmp_path / "session" / "runtime-verifier"
    saved.parent.mkdir()
    psql = ServerPsql(runtime_role)
    monkeypatch.setattr(subprocess, "run", psql)
    target = release(tmp_path / "release")

    assert bootstrap_database.switch_runtime(target, tmp_path / "secrets", saved)
    switched = runtime_verifier(runtime_role)
    assert bootstrap_database.scram_matches(switched, "app-secret")
    assert not bootstrap_database.scram_matches(switched, "previous-runtime-password")
    assert saved.read_text() == previous
    assert not bootstrap_database.switch_runtime(target, tmp_path / "secrets", saved)
    assert saved.read_text() == previous

    assert bootstrap_database.revert_runtime(target, saved)
    assert runtime_verifier(runtime_role) == previous
    assert not saved.exists()
    assert not bootstrap_database.revert_runtime(target, saved)
    assert runtime_verifier(runtime_role) == previous
    assert not any("app-secret" in script for script in psql.scripts)


def test_a_switch_from_a_role_without_password_reverts_to_none(
    runtime_role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_runtime_verifier(runtime_role, None)
    environment(tmp_path / "secrets")
    saved = tmp_path / "runtime-verifier"
    monkeypatch.setattr(subprocess, "run", ServerPsql(runtime_role))
    target = release(tmp_path / "release")

    assert bootstrap_database.switch_runtime(target, tmp_path / "secrets", saved)
    assert bootstrap_database.revert_runtime(target, saved)

    assert runtime_verifier(runtime_role) is None


@pytest.mark.parametrize("saved", ["x'; DROP ROLE pyracast_owner; --", "SCRAM-SHA-256$4096:x"])
def test_revert_refuses_a_saved_value_that_is_not_a_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved: str
) -> None:
    path = tmp_path / "runtime-verifier"
    path.write_text(saved)
    psql = Psql("")
    monkeypatch.setattr(subprocess, "run", psql)

    with pytest.raises(ValueError, match="verifier"):
        bootstrap_database.revert_runtime(release(tmp_path / "release"), path)
    assert psql.calls == []
    assert path.exists()


def test_switch_refuses_a_runtime_role_outside_the_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment(tmp_path / "secrets", app="postgres")
    psql = Psql("")
    monkeypatch.setattr(subprocess, "run", psql)

    with pytest.raises(ValueError, match="role names"):
        bootstrap_database.switch_runtime(
            release(tmp_path / "release"), tmp_path / "secrets", tmp_path / "saved"
        )
    assert psql.calls == []


@pytest.mark.parametrize(
    ("arguments", "call"),
    [
        (["converge", "release", "secrets"], ("converge_database", "release", "secrets")),
        (
            ["switch-runtime", "release", "secrets", "saved"],
            ("switch_runtime", "release", "secrets", "saved"),
        ),
        (["revert-runtime", "release", "saved"], ("revert_runtime", "release", "saved")),
    ],
)
def test_command_line_runs_each_step(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], call: tuple[str, ...]
) -> None:
    calls: list[tuple[str, ...]] = []
    for name in ("converge_database", "switch_runtime", "revert_runtime"):
        monkeypatch.setattr(
            bootstrap_database,
            name,
            lambda *paths, name=name: calls.append((name, *map(str, paths))) or [],
        )
    monkeypatch.setattr(sys, "argv", ["bootstrap_database.py", *arguments])

    bootstrap_database.main()

    assert calls == [call]
