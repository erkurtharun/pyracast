import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from django.conf import settings
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

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


@contextmanager
def cluster_roles() -> Iterator[None]:
    # Roles belong to the cluster, advisory locks to one database: lock in a shared database.
    with psycopg.connect(**server_parameters("postgres"), autocommit=True) as lock:
        lock.execute("SELECT pg_advisory_lock(hashtext('pyracast-bootstrap'))")
        try:
            yield
        finally:
            lock.execute("SELECT pg_advisory_unlock(hashtext('pyracast-bootstrap'))")


def provision(database: str) -> list[str]:
    with cluster_roles(), psycopg.connect(**server_parameters(database)) as connection:
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


def log_line(diagnostic: psycopg.errors.Diagnostic) -> str:
    # A parse tree arrives as the detail of its log line.
    return f"{diagnostic.message_primary or ''} {diagnostic.message_detail or ''}"


class ServerPsql:
    # psql opens its session with the startup options of its -d connection string, and -1 sends
    # BEGIN before the script and COMMIT after it. Records each log line the server sends it.
    def __init__(self, database: str) -> None:
        self.database = database
        self.commands: list[list[str]] = []
        self.scripts: list[str] = []
        self.emitted: list[str] = []

    def record(self, diagnostic: psycopg.errors.Diagnostic) -> None:
        self.emitted.append(log_line(diagnostic))

    def __call__(
        self, command: list[str], *, input: str | None = None, **options: Any
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        target = command[command.index("-d") + 1]
        startup = conninfo_to_dict(target).get("options") if "=" in target else None
        parameters = server_parameters(self.database) | ({"options": startup} if startup else {})
        with psycopg.connect(**parameters, autocommit=True) as connection:
            connection.add_notice_handler(self.record)
            if input is None:
                rows = connection.execute(command[-1].encode()).fetchall()
                stdout = "".join(f"{row[0]}\n" for row in rows)
                return subprocess.CompletedProcess(command, 0, stdout=stdout)
            self.scripts.append(input)
            single = "--single-transaction" in command
            if single:
                connection.execute("BEGIN")
            connection.execute(input.encode())
            if single:
                connection.execute("COMMIT")
        return subprocess.CompletedProcess(command, 0, stdout="")


def role_verifier(database: str, role: str = bootstrap_database.RUNTIME_ROLE) -> str | None:
    with psycopg.connect(**server_parameters(database)) as connection:
        (verifier,) = connection.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname = %s", (role,)
        ).fetchone() or (None,)
    return verifier


def set_role_verifier(
    database: str, verifier: str | None, role: str = bootstrap_database.RUNTIME_ROLE
) -> list[str]:
    emitted: list[str] = []
    with psycopg.connect(**server_parameters(database), autocommit=True) as connection:
        connection.add_notice_handler(lambda diagnostic: emitted.append(log_line(diagnostic)))
        connection.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(verifier))
        )
    return emitted


@pytest.fixture
def runtime_role(scratch_database: str) -> Iterator[str]:
    provision(scratch_database)
    with cluster_roles():
        original = role_verifier(scratch_database)
        try:
            yield scratch_database
        finally:
            set_role_verifier(scratch_database, original)


@pytest.mark.parametrize("previous", [None, "previous-runtime-password"])
def test_the_runtime_password_switch_sets_the_secret_once_and_keeps_nothing_else(
    runtime_role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previous: str | None
) -> None:
    set_role_verifier(runtime_role, previous and bootstrap_database.scram_verifier(previous, SALT))
    environment(tmp_path / "secrets")
    psql = ServerPsql(runtime_role)
    monkeypatch.setattr(subprocess, "run", psql)
    target = release(tmp_path / "release")
    before = sorted(tmp_path.rglob("*"))

    assert bootstrap_database.switch_runtime(target, tmp_path / "secrets")
    switched = role_verifier(runtime_role)
    assert bootstrap_database.scram_matches(switched, "app-secret")
    assert not bootstrap_database.switch_runtime(target, tmp_path / "secrets")
    assert role_verifier(runtime_role) == switched
    assert sorted(tmp_path.rglob("*")) == before
    assert not any("app-secret" in script for script in psql.scripts)


# Sessions inherit these from the database, as they would from postgresql.conf or ALTER SYSTEM;
# client_min_messages sends each log line the server writes for a session to its client too.
INHERITED_LOGGING = {
    "log_statement": "all",
    "log_min_duration_statement": "0",
    "log_min_duration_sample": "0",
    "log_transaction_sample_rate": "1",
    "debug_print_parse": "on",
    "client_min_messages": "log",
}


@pytest.fixture
def logging_database(scratch_database: str) -> Iterator[str]:
    provision(scratch_database)
    with psycopg.connect(**server_parameters("postgres"), autocommit=True) as server:
        for name, value in INHERITED_LOGGING.items():
            server.execute(
                sql.SQL("ALTER DATABASE {} SET {} = {}").format(
                    sql.Identifier(scratch_database), sql.Identifier(name), sql.Literal(value)
                )
            )
    with cluster_roles():
        roles = bootstrap_database.ROLES.values()
        originals = {role: role_verifier(scratch_database, role) for role in roles}
        try:
            yield scratch_database
        finally:
            for role, verifier in originals.items():
                set_role_verifier(scratch_database, verifier, role)


@pytest.mark.parametrize(
    ("step", "role", "secret"),
    [("converge", "pyracast_owner", "owner-secret"), ("switch", "pyracast_app", "app-secret")],
)
def test_password_changes_run_in_a_session_whose_statements_postgresql_never_logs(
    logging_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    role: str,
    secret: str,
) -> None:
    control = ServerPsql(logging_database)
    control(["psql", "-d", "pyracast", "-Atc", "SELECT 'logged-control'"])
    assert any("logged-control" in line for line in control.emitted)
    set_role_verifier(logging_database, None, role)
    environment(tmp_path / "secrets")
    psql = ServerPsql(logging_database)
    monkeypatch.setattr(subprocess, "run", psql)
    target = release(tmp_path / "release")

    if step == "converge":
        bootstrap_database.converge_database(target, tmp_path / "secrets")
    else:
        bootstrap_database.switch_runtime(target, tmp_path / "secrets")

    assert bootstrap_database.scram_matches(role_verifier(logging_database, role), secret)
    leaked = re.compile("scram|secret", re.IGNORECASE)
    assert [line for line in psql.emitted if leaked.search(line)] == []
    assert not any("SCRAM" in argument for command in psql.commands for argument in command)


@pytest.mark.parametrize("role", sorted(bootstrap_database.ROLES.values()))
def test_restoring_a_role_verifier_logs_nothing_where_every_statement_is_logged(
    logging_database: str, role: str
) -> None:
    control = ServerPsql(logging_database)
    control(["psql", "-d", "pyracast", "-Atc", "SELECT 'logged-control'"])
    assert any("logged-control" in line for line in control.emitted)
    verifier = bootstrap_database.scram_verifier("restored-secret", SALT)

    emitted = set_role_verifier(logging_database, verifier, role)

    assert role_verifier(logging_database, role) == verifier
    assert [line for line in emitted if "SCRAM-SHA-256" in line] == []


def test_switch_refuses_a_runtime_role_outside_the_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment(tmp_path / "secrets", app="postgres")
    psql = Psql("")
    monkeypatch.setattr(subprocess, "run", psql)

    with pytest.raises(ValueError, match="role names"):
        bootstrap_database.switch_runtime(release(tmp_path / "release"), tmp_path / "secrets")
    assert psql.calls == []


@pytest.mark.parametrize(
    ("step", "function"), [("converge", "converge_database"), ("switch-runtime", "switch_runtime")]
)
def test_command_line_runs_each_step(
    monkeypatch: pytest.MonkeyPatch, step: str, function: str
) -> None:
    calls: list[tuple[str, ...]] = []

    def recorder(name: str) -> Callable[..., list[str]]:
        def record(*paths: Path) -> list[str]:
            calls.append((name, *map(str, paths)))
            return []

        return record

    for name in ("converge_database", "switch_runtime"):
        monkeypatch.setattr(bootstrap_database, name, recorder(name))
    monkeypatch.setattr(sys, "argv", ["bootstrap_database.py", step, "release", "secrets"])

    bootstrap_database.main()

    assert calls == [(function, "release", "secrets")]
