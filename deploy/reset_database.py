# ruff: noqa: S603, S607 -- Fixed Docker, curl, gzip, sync and systemctl executables with argument lists.
import argparse
import base64
import dataclasses
import datetime
import gzip
import hashlib
import importlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from functools import partial
from pathlib import Path
from types import FrameType
from typing import Literal
from urllib.parse import quote

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy.backup import compose_command, upload_backup
from deploy.operations_log import access_token

# The installer's drain order: ingress first, then the producer, then the consumers.
STOP_ORDER = (
    ("660", ("dispatcher",)),
    ("660", ("worker", "notifications")),
    ("100", ("gateway", "api", "web")),
)
APPLICATION_SERVICES = frozenset(service for _, group in STOP_ORDER for service in group)
SUPPORT_SERVICES = ("postgres", "rabbitmq")
OBJECT_PATTERN = re.compile(r"postgres/[A-Za-z0-9/_.-]+\.dump")
PRERESET = "-prereset-"
BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")
# Roles and their memberships belong to the cluster and survive DROP DATABASE.
ROLE_STATEMENTS = ("CREATE ROLE ", "GRANT pyracast_owner TO ")
COUNT_SQL = (
    "SELECT c.relname || ' ' || (xpath('/row/n/text()', query_to_xml("
    "format('SELECT count(*) AS n FROM public.%I', c.relname), false, true, '')))[1]::text "
    "FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace "
    "WHERE s.nspname = 'public' AND c.relkind = 'r' ORDER BY c.relname"
)
SOLCAST_COUNT_SQL = (
    "SELECT count(*) FROM forecasts_forecastsnapshot s "
    "JOIN forecasts_collectionjob j ON j.id = s.job_id "
    "JOIN connections_sitebinding b ON b.id = j.binding_id "
    "JOIN connections_provideraccount a ON a.id = b.provider_account_id "
    "WHERE a.provider = 'solcast'"
)
# Same filter as SOLCAST_COUNT_SQL. CSV with control-byte delimiter and quote never quotes JSON
# text, so each line is the JSON as is.
SOLCAST_EXPORT_SQL = (
    "COPY (SELECT row_to_json(e) FROM (SELECT s.id, a.provider, s.source_kind, j.endpoint, "
    "s.issued_at, s.collected_at, s.coverage_start, s.coverage_end, s.raw_payload, "
    "s.normalized_intervals, s.weather_payload, ARRAY(SELECT DISTINCT c.plant_id "
    "FROM plants_plantconfiguration c WHERE c.binding_id = b.id ORDER BY c.plant_id) "
    "AS plant_ids, row_to_json(b) AS binding FROM forecasts_forecastsnapshot s "
    "JOIN forecasts_collectionjob j ON j.id = s.job_id "
    "JOIN connections_sitebinding b ON b.id = j.binding_id "
    "JOIN connections_provideraccount a ON a.id = b.provider_account_id "
    "WHERE a.provider = 'solcast') e ORDER BY e.id) "
    "TO STDOUT WITH (FORMAT csv, DELIMITER E'\\x02', QUOTE E'\\x01', ENCODING 'UTF8')"
)
ROLE_CHECK_SQL = """DO $$
BEGIN
  IF NOT has_database_privilege('pyracast_app', 'pyracast', 'CONNECT') THEN
    RAISE EXCEPTION 'pyracast_app cannot connect to database pyracast';
  END IF;
  IF has_database_privilege('pyracast_owner', 'pyracast', 'CREATE') THEN
    RAISE EXCEPTION 'pyracast_owner keeps CREATE on database pyracast';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_extension WHERE extname = 'btree_gist') THEN
    RAISE EXCEPTION 'extension btree_gist is missing';
  END IF;
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public'
             AND tableowner <> 'pyracast_owner') THEN
    RAISE EXCEPTION 'a table in schema public is not owned by pyracast_owner';
  END IF;
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public'
             AND NOT has_table_privilege('pyracast_app', format('%I.%I', schemaname, tablename),
                                         'SELECT')) THEN
    RAISE EXCEPTION 'pyracast_app cannot read every table in schema public';
  END IF;
END $$;
BEGIN;
SET ROLE pyracast_owner;
CREATE TABLE pyracast_reset_probe (id integer);
ROLLBACK;
"""
CONNECT_CHECK = (
    "import django; django.setup(); "
    "from django.db import connection; connection.ensure_connection()"
)


class RefusedError(Exception):
    pass


type MarkerState = Literal["held", "foreign", "missing", "unknown"]
type SignalHandler = Callable[[int, FrameType | None], object] | int | signal.Handlers | None

MARKER_REPORT: dict[MarkerState, str] = {
    "held": "The maintenance marker {marker} is present and holds {identity}.",
    "foreign": "The maintenance marker {marker} is present but does not hold {identity}.",
    "missing": "The maintenance marker {marker} is missing.",
    "unknown": "The maintenance marker {marker} could not be read.",
}


def describe(error: BaseException) -> str:
    text = str(error) or type(error).__name__
    if (
        isinstance(error, subprocess.CalledProcessError | subprocess.TimeoutExpired)
        and error.stderr
    ):
        detail = error.stderr
        if isinstance(detail, bytes):  # TimeoutExpired keeps raw bytes even with text=True.
            detail = detail.decode(errors="replace")
        text += "\n" + detail.strip()[-2000:]
    return text


def outcome(what: str, error: BaseException) -> str:
    if isinstance(error, KeyboardInterrupt):
        return f"{what} was interrupted."
    return f"{what} failed: {describe(error)}"


class StartFailedError(RuntimeError):
    def __init__(
        self,
        failure: BaseException,
        problems: list[tuple[str, BaseException]],
        identity: str,
        marker: MarkerState,
        running: set[str] | None,
        signals: list[str],
        handlers: dict[signal.Signals, SignalHandler],
    ) -> None:
        super().__init__(describe(failure))
        self.failure = failure
        self.problems = problems
        self.identity = identity
        self.marker = marker
        self.running = running
        self.signals = signals
        self.handlers = handlers

    @property
    def interrupted(self) -> bool:
        errors = [self.failure, *(error for _, error in self.problems)]
        return bool(self.signals) or any(isinstance(error, KeyboardInterrupt) for error in errors)


@dataclasses.dataclass(frozen=True)
class Session:
    """One maintenance window: valid only while the marker still holds its identity."""

    marker: str
    object: str
    release: str
    replaced: bool


@dataclasses.dataclass(frozen=True)
class Host:
    config: Path
    release: Path
    bucket: str
    runtime_version: str
    marker: Path = Path("/var/lib/pyracast/release-blocked")
    lock: Path = Path("/run/lock/pyracast-release.lock")
    state: Path = Path("/var/lib/pyracast/reset")
    environment: Path = Path("/run/pyracast")

    @property
    def session(self) -> Path:
        return self.state / "session.json"

    def command(self, arguments: str) -> str:
        return f"python3 {self.release}/deploy/reset_database.py {self.config} {arguments}"


@dataclasses.dataclass(frozen=True)
class SolcastExport:
    object: str
    sha256: str
    rows: int


@dataclasses.dataclass(frozen=True)
class Record:
    object: str
    sha256: str
    counts: dict[str, int]
    solcast: SolcastExport | None = None


def load_host(config_path: Path) -> Host:
    config = json.loads(config_path.read_text())
    bucket = config["backup_bucket"]
    if not BUCKET_PATTERN.fullmatch(bucket):
        raise ValueError("Invalid backup bucket.")
    return Host(
        config=config_path,
        release=Path("/opt/pyracast/current"),
        bucket=bucket,
        runtime_version=str(config["runtime_secret_version"]),
    )


def step(message: str) -> None:
    print(f"==> {message}", flush=True)


def print_safely(text: str, *, error: bool = False) -> None:
    with suppress(OSError, ValueError):
        print(text, file=sys.stderr if error else sys.stdout, flush=True)


def cleanup_step(message: str) -> None:
    print_safely(f"==> {message}")


def compose(host: Host) -> list[str]:
    return compose_command(host.release)


def capture(command: list[str], *, script: str = "", timeout: int = 1200) -> str:
    return subprocess.run(
        command, input=script, check=True, capture_output=True, text=True, timeout=timeout
    ).stdout


def psql(host: Host, database: str, *arguments: str, script: str = "") -> str:
    command = [*compose(host), "exec", "-T", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1"]
    return capture([*command, "-U", "postgres", "-d", database, *arguments], script=script)


def parse_counts(output: str) -> dict[str, int]:
    rows = (line.rsplit(" ", 1) for line in output.splitlines() if line.strip())
    return {name: int(count) for name, count in rows}


def table_counts(host: Host) -> dict[str, int]:
    return parse_counts(psql(host, "pyracast", "-At", "-c", COUNT_SQL))


def require_equal_counts(expected: dict[str, int], actual: dict[str, int], what: str) -> None:
    differences = [
        f"{name}: expected {expected.get(name)}, found {actual.get(name)}"
        for name in sorted(expected.keys() | actual.keys())
        if expected.get(name) != actual.get(name)
    ]
    if differences:
        raise RuntimeError(f"{what} row counts differ: " + "; ".join(differences))


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def archive_path(host: Host, name: str) -> Path:
    return host.state / name.rsplit("/", 1)[-1]


def record_path(host: Host, name: str) -> Path:
    return archive_path(host, name).with_suffix(".json")


def sync(path: Path) -> None:
    subprocess.run(["sync", "-f", str(path)], check=True)


def write_synced(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".next")
    temporary.write_text(text)
    temporary.replace(path)
    sync(path)
    sync(path.parent)


def save_record(host: Host, record: Record) -> None:
    text = json.dumps(dataclasses.asdict(record), indent=2, sort_keys=True)
    write_synced(record_path(host, record.object), text)


def load_record(host: Host, name: str) -> Record | None:
    path = record_path(host, name)
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    counts = {str(table): int(count) for table, count in data["counts"].items()}
    export = data.get("solcast")
    solcast = (
        None
        if export is None
        else SolcastExport(
            object=str(export["object"]), sha256=str(export["sha256"]), rows=int(export["rows"])
        )
    )
    return Record(
        object=str(data["object"]), sha256=str(data["sha256"]), counts=counts, solcast=solcast
    )


def require_object_name(name: str) -> None:
    if not OBJECT_PATTERN.fullmatch(name):
        raise RefusedError(f"{name!r} is not a PostgreSQL archive object name (postgres/....dump).")


def require_kept(path: Path, sha256: str, what: str) -> None:
    if not path.is_file():
        raise RefusedError(
            f"The verified {what} {path} is missing; copy it back to that path first."
        )
    if file_sha256(path) != sha256:
        raise RefusedError(f"The {what} {path} no longer matches its recorded SHA-256 {sha256}.")


def verified_record(host: Host, name: str) -> tuple[Record, Path]:
    record = load_record(host, name)
    if record is None:
        raise RefusedError(
            f"No verified archive {name} on this host. "
            "Name the object exactly as prepare printed it."
        )
    if record.object != name:
        raise RefusedError(
            f"The archive kept as {archive_path(host, name)} is {record.object}, not {name}."
        )
    archive = archive_path(host, name)
    require_kept(archive, record.sha256, "archive")
    if PRERESET in name:
        if record.solcast is None:
            raise RefusedError(
                f"The record of {name} names no verified Solcast export; "
                "prepare keeps one beside each pre-reset archive."
            )
        export = archive_path(host, record.solcast.object)
        require_kept(export, record.solcast.sha256, "Solcast export")
    return record, archive


def release_values(host: Host) -> dict[str, str]:
    lines = (host.release / "deploy/release.env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line)


def site_ready(host: Host, attempts: int) -> bool:
    site = release_values(host)["SITE_ADDRESS"]
    result = subprocess.run(
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--noproxy",
            "*",
            "--connect-timeout",
            "5",
            "--max-time",
            "10",
            "--retry",
            str(attempts),
            "--retry-delay",
            "5",
            "--retry-max-time",
            "180",
            "--retry-all-errors",
            "--resolve",
            f"{site}:443:127.0.0.1",
            f"https://{site}/health/ready/",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "ok"


@contextmanager
def release_lock(host: Host) -> Iterator[None]:
    locks = importlib.import_module("fcntl")
    host.lock.parent.mkdir(parents=True, exist_ok=True)
    with host.lock.open("a") as handle:
        try:
            locks.flock(handle, locks.LOCK_EX | locks.LOCK_NB)
        except BlockingIOError:
            raise RefusedError(
                "Another release, backup or watchdog operation holds the release lock; "
                "wait a minute and run the same command again."
            ) from None
        yield


def current_session(host: Host) -> Session | None:
    # The installer touches an existing marker and removes it after a successful release, so a
    # session whose identity the marker no longer holds belongs to a maintenance that has ended.
    if not host.session.is_file() or not host.marker.is_file():
        return None
    session = Session(**json.loads(host.session.read_text()))
    return session if host.marker.read_text().strip() == session.marker else None


def save_session(host: Host, session: Session) -> None:
    write_synced(host.session, json.dumps(dataclasses.asdict(session), indent=2, sort_keys=True))


def marker_identity(host: Host) -> str:
    identity = host.marker.read_text().strip()
    if not identity:
        identity = uuid.uuid4().hex
        write_synced(host.marker, identity)
    return identity


def status_command(host: Host) -> list[str]:
    command = [*compose(host), "ps", "--services"]
    for status in ("running", "restarting", "paused"):
        command += ["--status", status]
    return command


def running_services(host: Host) -> set[str]:
    return set(capture(status_command(host)).split())


def stop_group(host: Host, timeout: str, services: tuple[str, ...]) -> None:
    subprocess.run(
        [*compose(host), "stop", "-t", timeout, *services],
        check=True,
        timeout=int(timeout) + 60,
    )


def stop_in_order(host: Host) -> None:
    for timeout, services in STOP_ORDER:
        step(f"Stopping {', '.join(services)}")
        stop_group(host, timeout, services)


def stop_application(host: Host) -> None:
    stop_in_order(host)
    running = running_services(host) & APPLICATION_SERVICES
    if running:
        raise RuntimeError(f"Application containers still run: {', '.join(sorted(running))}.")


def require_maintenance(host: Host) -> None:
    if not host.marker.exists():
        raise RefusedError(f"{host.marker} does not exist; this step runs only during maintenance.")
    running = running_services(host) & APPLICATION_SERVICES
    if running:
        raise RefusedError(
            f"Application containers still run: {', '.join(sorted(running))}. "
            "This step runs only while the application is stopped."
        )


def write_runtime_secrets(host: Host) -> None:
    step(f"Writing the runtime secrets with {host.release}/deploy/runtime_secrets.py")
    subprocess.run(
        [sys.executable, str(host.release / "deploy/runtime_secrets.py"), str(host.config)],
        check=True,
    )


def start_support(host: Host) -> None:
    subprocess.run(
        [*compose(host), "up", "--detach", "--wait", "--wait-timeout", "180", *SUPPORT_SERVICES],
        check=True,
        timeout=240,
    )


def require_roles(host: Host) -> None:
    present = psql(
        host,
        "postgres",
        "-At",
        "-c",
        "SELECT count(*) FROM pg_roles WHERE rolname IN ('pyracast_owner', 'pyracast_app')",
    )
    if present.strip() != "2":
        raise RefusedError("The roles pyracast_owner and pyracast_app do not both exist.")


def dump_database(host: Host) -> tuple[str, Path]:
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y/%m/%d/%Y%m%dT%H%M%SZ")
    name = f"postgres/{stamp}{PRERESET}{uuid.uuid4().hex}.dump"
    archive = archive_path(host, name)
    partial = archive.with_suffix(".partial")
    with partial.open("wb") as stream:
        subprocess.run(
            [
                *compose(host),
                "exec",
                "-T",
                "postgres",
                "pg_dump",
                "-U",
                "postgres",
                "-d",
                "pyracast",
                "--format=custom",
                "--compress=zstd:3",
            ],
            stdout=stream,
            check=True,
            timeout=1200,
        )
    partial.replace(archive)
    return name, archive


def wait_until_ready(container: str) -> None:
    probe = ["docker", "exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"]
    for _attempt in range(60):
        if subprocess.run([*probe, "-d", "pyracast"], capture_output=True).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("The throwaway PostgreSQL did not become ready.")


@contextmanager
def throwaway_copy(host: Host, archive: Path) -> Iterator[Callable[[str], str]]:
    configuration = json.loads(capture([*compose(host), "config", "--format", "json"]))
    image = configuration["services"]["postgres"]["image"]
    container = "pyracast-reset-" + uuid.uuid4().hex
    volume = container + "-data"
    connection = ["-h", "127.0.0.1", "-U", "postgres", "-d", "pyracast"]
    try:
        subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
        subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container,
                "--network",
                "none",
                "--memory",
                "768m",
                "--pids-limit",
                "100",
                "--security-opt",
                "no-new-privileges:true",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "--env",
                "POSTGRES_DB=pyracast",
                "--volume",
                f"{volume}:/var/lib/postgresql",
                image,
            ],
            check=True,
            capture_output=True,
        )
        wait_until_ready(container)
        with archive.open("rb") as stream:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "pg_restore",
                    *connection,
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    "--no-acl",
                ],
                stdin=stream,
                check=True,
                capture_output=True,
                timeout=1200,
            )
        query = ["docker", "exec", container, "psql", "-X", "-v", "ON_ERROR_STOP=1", *connection]
        yield lambda sql: capture([*query, "-At", "-c", sql])
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=False)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True, check=False)


def verify_archive(host: Host, archive: Path) -> dict[str, int]:
    with throwaway_copy(host, archive) as query:
        return parse_counts(query(COUNT_SQL))


def export_name(archive: str) -> str:
    return archive.removesuffix(".dump") + ".solcast.jsonl.gz"


def exported_rows(path: Path, counted: int) -> int:
    rows = 0
    try:
        # A zero-byte file reads as an empty gzip stream.
        if path.stat().st_size == 0:
            raise ValueError("the file holds no gzip member")
        with gzip.open(path, "rb") as lines:
            for line in lines:
                if not isinstance(json.loads(line), dict):
                    raise ValueError("a line is not a JSON object")
                rows += 1
    except (OSError, EOFError, ValueError, zlib.error) as error:
        raise RuntimeError(f"{path} is not gzip-compressed JSON lines: {error}") from error
    if rows != counted:
        raise RuntimeError(
            f"The Solcast export holds {rows} snapshots; the database counts {counted}."
        )
    return rows


def export_solcast(host: Host, archive: str) -> SolcastExport:
    name = export_name(archive)
    packed = archive_path(host, name)
    lines = packed.with_suffix("")
    step("Exporting the Solcast snapshots")
    with lines.open("wb") as stream:
        subprocess.run(
            [
                *compose(host),
                "exec",
                "-T",
                "--env",
                "PGTZ=UTC",
                "postgres",
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                "-d",
                "pyracast",
                "-c",
                SOLCAST_EXPORT_SQL,
            ],
            stdout=stream,
            check=True,
            timeout=1200,
        )
    subprocess.run(["gzip", "--force", str(lines)], check=True, timeout=1200)
    counted = int(psql(host, "pyracast", "-At", "-c", SOLCAST_COUNT_SQL))
    rows = exported_rows(packed, counted)
    step(f"Uploading gs://{host.bucket}/{name}")
    upload_backup(host.bucket, name, packed)
    return SolcastExport(object=name, sha256=file_sha256(packed), rows=rows)


def download_archive(host: Host, name: str) -> Path:
    path = f"{host.bucket}/o/{quote(name, safe='')}"
    headers = {"Authorization": f"Bearer {access_token()}"}
    described = urllib.request.Request(
        f"https://storage.googleapis.com/storage/v1/b/{path}", headers=headers
    )
    with urllib.request.urlopen(described, timeout=30) as reply:  # noqa: S310
        metadata = json.load(reply)
    archive = archive_path(host, name)
    partial = archive.with_suffix(".partial")
    media = urllib.request.Request(
        f"https://storage.googleapis.com/storage/v1/b/{path}?alt=media", headers=headers
    )
    with urllib.request.urlopen(media, timeout=300) as reply, partial.open("wb") as stream:  # noqa: S310
        while block := reply.read(1024 * 1024):
            stream.write(block)
    with partial.open("rb") as stream:
        md5 = base64.b64encode(hashlib.file_digest(stream, "md5").digest()).decode()
    if (
        not isinstance(metadata, dict)
        or metadata.get("md5Hash") != md5
        or metadata.get("size") != str(partial.stat().st_size)
    ):
        partial.unlink()
        raise RefusedError(f"The download of {name} does not match the bucket's checksum.")
    partial.replace(archive)
    return archive


def fetch_export(host: Host, archive: str, counted: int) -> SolcastExport:
    name = export_name(archive)
    step(f"Downloading gs://{host.bucket}/{name}")
    path = download_archive(host, name)
    rows = exported_rows(path, counted)
    return SolcastExport(object=name, sha256=file_sha256(path), rows=rows)


def fetch_record(host: Host, name: str) -> None:
    step(f"Downloading gs://{host.bucket}/{name}")
    archive = download_archive(host, name)
    step("Restoring it into a throwaway PostgreSQL")
    with throwaway_copy(host, archive) as query:
        counts = parse_counts(query(COUNT_SQL))
        snapshots = int(query(SOLCAST_COUNT_SQL)) if PRERESET in name else None
    solcast = None if snapshots is None else fetch_export(host, name, snapshots)
    record = Record(object=name, sha256=file_sha256(archive), counts=counts, solcast=solcast)
    save_record(host, record)


def queue_messages(host: Host) -> dict[str, int]:
    output = capture(
        [
            *compose(host),
            "exec",
            "-T",
            "rabbitmq",
            "rabbitmqctl",
            "list_queues",
            "--quiet",
            "--no-table-headers",
            "name",
            "messages",
        ]
    )
    rows = (line.split("\t") for line in output.splitlines() if line.strip())
    return {name: int(count) for name, count in rows}


def purge_queues(host: Host) -> None:
    # New primary keys restart at 1, so a leftover message would address an unrelated row.
    queues = sorted(queue_messages(host))
    for name in queues:
        capture([*compose(host), "exec", "-T", "rabbitmq", "rabbitmqctl", "purge_queue", name])
    remaining = {name: count for name, count in queue_messages(host).items() if count}
    if remaining:
        listed = ", ".join(f"{name}: {count}" for name, count in sorted(remaining.items()))
        raise RuntimeError(f"Queues still hold messages after the purge ({listed}).")
    step(f"Purged {len(queues)} queues: {', '.join(queues)}")


def database_bootstrap(statements: str) -> str:
    lines = statements.splitlines()
    return "\n".join(line for line in lines if not line.startswith(ROLE_STATEMENTS)) + "\n"


def recreate_database(host: Host) -> None:
    step("Dropping and recreating database pyracast")
    psql(
        host,
        "postgres",
        "-c",
        "DROP DATABASE IF EXISTS pyracast WITH (FORCE)",
        "-c",
        "CREATE DATABASE pyracast",
    )
    step("Applying the database part of deploy/bootstrap.sql")
    bootstrap = (host.release / "deploy/bootstrap.sql").read_text()
    psql(host, "pyracast", script=database_bootstrap(bootstrap))


def check_roles(host: Host) -> None:
    step("Checking the owner and runtime roles")
    psql(host, "pyracast", script=ROLE_CHECK_SQL)
    subprocess.run(
        [*compose(host), "run", "--rm", "--no-deps", "api", "python", "-c", CONNECT_CHECK],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )


def restore_live(host: Host, archive: Path) -> None:
    step(f"Restoring {archive.name} with owners and privileges")
    with archive.open("rb") as stream:
        subprocess.run(
            [
                *compose(host),
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "-U",
                "postgres",
                "-d",
                "pyracast",
                "--exit-on-error",
                "--single-transaction",
            ],
            stdin=stream,
            check=True,
            capture_output=True,
            timeout=1200,
        )


def release_serves(host: Host) -> bool:
    step(f"Checking that the release in {host.release} has every migration it needs applied")
    result = subprocess.run(
        [*compose(host), "run", "--rm", "--no-deps", "api", "pyracast", "migrate", "--check"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    return result.returncode == 0


def start_release(host: Host) -> None:
    step(f"Starting the release in {host.release}")
    identity = host.marker.read_text()
    try:
        subprocess.run(
            [*compose(host), "up", "--detach", "--wait", "--wait-timeout", "180"],
            check=True,
            timeout=300,
        )
        if not site_ready(host, attempts=18):
            raise RuntimeError("The release started, but /health/ready/ did not answer ok.")
        host.marker.unlink()
        sync(host.marker.parent)
        subprocess.run(["systemctl", "reset-failed", "pyracast.service"], check=True)
        subprocess.run(["systemctl", "start", "pyracast.service"], check=True)
    except BaseException as failure:
        raise stop_after_failed_start(host, identity, failure) from failure


def attempt[T](
    problems: list[tuple[str, BaseException]], what: str, action: Callable[[], T]
) -> T | None:
    try:
        return action()
    except BaseException as error:  # Ctrl-C included: every cleanup step still runs.
        problems.append((what, error))
        return None


def marker_state(host: Host, identity: str) -> MarkerState:
    try:
        content = host.marker.read_text()
    except FileNotFoundError:
        return "missing"
    return "held" if content.strip() == identity.strip() else "foreign"


def defer_signals(received: list[str]) -> dict[signal.Signals, SignalHandler]:
    def record(number: int, _frame: FrameType | None) -> None:
        received.append(signal.Signals(number).name)

    # Main thread only. Python's raising Ctrl-C default could skip a cleanup step or lose the
    # report, so the handlers stay deferred until main has printed it and calls restore_signals.
    return {number: signal.signal(number, record) for number in (signal.SIGINT, signal.SIGTERM)}


def restore_signals(handlers: dict[signal.Signals, SignalHandler], received: list[str]) -> None:
    try:
        signal.signal(signal.SIGTERM, handlers[signal.SIGTERM])
    finally:
        try:
            signal.signal(signal.SIGINT, handlers[signal.SIGINT])
        except KeyboardInterrupt:
            received.append("SIGINT")


def stop_after_failed_start(host: Host, identity: str, failure: BaseException) -> StartFailedError:
    received: list[str] = []
    handlers = defer_signals(received)
    cleanup_step("Writing the maintenance marker back and stopping the application again")
    problems: list[tuple[str, BaseException]] = []
    attempt(
        problems, "Writing the maintenance marker back", lambda: write_synced(host.marker, identity)
    )
    for timeout, services in STOP_ORDER:
        names = ", ".join(services)
        cleanup_step(f"Stopping {names}")
        attempt(problems, f"Stopping {names}", partial(stop_group, host, timeout, services))
    running = attempt(
        problems,
        "Listing the running containers",
        lambda: running_services(host) & APPLICATION_SERVICES,
    )
    marker = attempt(
        problems, "Reading the maintenance marker", lambda: marker_state(host, identity)
    )
    return StartFailedError(
        failure, problems, identity, marker or "unknown", running, received, handlers
    )


def end_session(host: Host) -> None:
    host.session.unlink(missing_ok=True)


def print_prepared(host: Host, record: Record, solcast: SolcastExport) -> None:
    print("\nPre-reset archive verified: the restored copy has every table's live row count.")
    print(f"  Object:  gs://{host.bucket}/{record.object}")
    print(f"  SHA-256: {record.sha256}")
    print(f"  Archive: {archive_path(host, record.object)}")
    print(f"  Solcast export: gs://{host.bucket}/{solcast.object}")
    print(f"  Solcast SHA-256: {solcast.sha256}")
    print(f"  Solcast snapshots: {solcast.rows}")
    print(f"  Release commit: {host.release.resolve().name}")
    for key, value in sorted(release_values(host).items()):
        if key.endswith("_IMAGE"):
            print(f"  {key}={value}")
    print(f"  Runtime secret version: {host.runtime_version}")
    print(f"  Tables: {len(record.counts)}, rows: {sum(record.counts.values())}")
    for table, count in sorted(record.counts.items()):
        print(f"    {table} {count}")
    kept = archive_path(host, record.object).name
    earlier = sorted(path.name for path in host.state.glob("*.dump") if path.name != kept)
    if earlier:
        print(f"Earlier archives in {host.state}: {', '.join(earlier)}.")
        print(
            "Delete each with its .json record and Solcast export once their off-host copies "
            "are verified."
        )
    print("The site is down. Copy the archive and the Solcast export off the host, then run:")
    print(f"  {host.command(f'reset --confirm {record.object}')}")
    print("To serve the previous release again instead, run:")
    print(f"  {host.command('abort')}")


def prepare(host: Host) -> None:
    with release_lock(host):
        if host.marker.exists():
            session = current_session(host)
            if session is not None and session.replaced:
                recovery = host.command(f"restore --confirm {session.object}")
            else:
                recovery = host.command("abort")
            raise RefusedError(
                f"{host.marker} exists: the site is in maintenance. After an interrupted prepare "
                f"run {recovery}; after a failed release follow 'A release that fails during "
                "maintenance' in the deployment guide."
            )
        if not site_ready(host, attempts=0):
            raise RefusedError("The site does not answer /health/ready/ with ok.")
        host.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        step(f"Blocking automatic starts with {host.marker}")
        identity = uuid.uuid4().hex
        write_synced(host.marker, identity)
        stop_application(host)
        step("Dumping database pyracast")
        name, archive = dump_database(host)
        digest = file_sha256(archive)
        step(f"Uploading gs://{host.bucket}/{name}")
        upload_backup(host.bucket, name, archive)
        live = table_counts(host)
        step("Restoring the archive into a throwaway PostgreSQL")
        require_equal_counts(live, verify_archive(host, archive), "Live and restored")
        solcast = export_solcast(host, name)
        record = Record(object=name, sha256=digest, counts=live, solcast=solcast)
        save_record(host, record)
        release = host.release.resolve().name
        save_session(host, Session(marker=identity, object=name, release=release, replaced=False))
    print_prepared(host, record, solcast)


def abort(host: Host) -> None:
    with release_lock(host):
        if not host.marker.exists():
            raise RefusedError(f"{host.marker} does not exist; abort runs only during maintenance.")
        session = current_session(host)
        if session is not None and session.replaced:
            raise RefusedError(
                "reset replaced the database in this maintenance; starting the previous release "
                "now would serve it without its data. Run "
                f"{host.command(f'restore --confirm {session.object}')}"
            )
        write_runtime_secrets(host)
        start_support(host)
        if not release_serves(host):
            raise RefusedError(
                f"The release in {host.release} cannot serve database pyracast: migrate --check "
                "reports unapplied migrations. Bring the data back with restore --confirm OBJECT "
                "(see 'A release that fails during maintenance' in the deployment guide)."
            )
        start_release(host)
        end_session(host)
    print("The previous release serves again and the marker is removed. Nothing else to do.")


def reset(host: Host, name: str) -> None:
    require_object_name(name)
    with release_lock(host):
        require_maintenance(host)
        session = current_session(host)
        if session is None or session.object != name:
            raise RefusedError(
                f"{name} is not the archive that prepare verified in this maintenance. "
                "Run prepare first and confirm the object it printed."
            )
        verified_record(host, name)
        write_runtime_secrets(host)
        start_support(host)
        require_roles(host)
        purge_queues(host)
        save_session(host, dataclasses.replace(session, replaced=True))
        recreate_database(host)
        check_roles(host)
    print("\nDatabase pyracast is empty, provisioned and reachable by both roles.")
    print(f"The marker {host.marker} stays: the site is down until the next release.")
    print("Next: start the Release workflow on main (GitHub, Actions, Release, Run workflow).")
    print("If that release fails, bring the data back with:")
    print(f"  {host.command(f'restore --confirm {name}')}")


def restore(host: Host, name: str) -> None:
    require_object_name(name)
    with release_lock(host):
        require_maintenance(host)
        session = current_session(host)
        if session is not None and session.replaced and session.object != name:
            raise RefusedError(
                f"reset replaced the database in this maintenance; only {session.object} holds "
                f"its data. Run {host.command(f'restore --confirm {session.object}')}"
            )
        host.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_runtime_secrets(host)
        if load_record(host, name) is None:
            fetch_record(host, name)
        record, archive = verified_record(host, name)
        if not record.counts.get("django_migrations"):
            raise RefusedError(
                f"{name} holds no applied migration, so no release can serve it. Name the archive "
                "prepare printed, or the pre-release dump of a release that failed without a reset."
            )
        start_support(host)
        require_roles(host)
        purge_queues(host)
        release = host.release.resolve().name
        identity = marker_identity(host)
        save_session(host, Session(marker=identity, object=name, release=release, replaced=True))
        recreate_database(host)
        restore_live(host, archive)
        require_equal_counts(record.counts, table_counts(host), "Archived and restored")
        check_roles(host)
        if not release_serves(host):
            raise RuntimeError(
                f"The release in {host.release} cannot serve the restored database: migrate "
                "--check reports unapplied migrations. Restore an archive that this release wrote."
            )
        start_release(host)
        end_session(host)
    print(f"\n{name} is restored and the previous release serves again; the marker is removed.")


RECOVERY = {
    "prepare": "The marker exists and the site is down: run {abort} to serve the previous release "
    "again, then run prepare again.",
    "abort": "The marker stays and the site is down. Inspect the containers and run {abort} again.",
    "reset": "The marker stays and the site is down. Every step repeats safely: run the same reset "
    "command again, or bring the data back with {restore}.",
    "restore": "The marker stays and the site is down. Every step repeats safely from the verified "
    "archive: run {restore} again.",
}


def print_recovery(host: Host, options: argparse.Namespace) -> None:
    confirm = getattr(options, "confirm", "OBJECT")
    print(
        RECOVERY[options.command].format(
            abort=host.command("abort"), restore=host.command(f"restore --confirm {confirm}")
        ),
        file=sys.stderr,
    )


def stop_outcome(problem: BaseException | None) -> str:
    if problem is None:
        return "done"
    return "interrupted" if isinstance(problem, KeyboardInterrupt) else "failed"


def print_failed_start(host: Host, options: argparse.Namespace, error: StartFailedError) -> None:
    rerun = "abort" if options.command == "abort" else f"restore --confirm {options.confirm}"
    shown = shlex.quote(error.identity.strip())
    lines = [outcome(options.command, error.failure)]
    lines += [outcome(what, problem) for what, problem in error.problems]
    lines += [
        f"{name} arrived after the start failed; every cleanup step was still attempted."
        for name in dict.fromkeys(error.signals)
    ]
    failed = dict(error.problems)
    stops = [
        f"{names} {stop_outcome(failed.get(f'Stopping {names}'))}"
        for names in (", ".join(services) for _, services in STOP_ORDER)
    ]
    lines.append("Stop commands in the installer's order: " + "; ".join(stops) + ".")
    lines.append(MARKER_REPORT[error.marker].format(marker=host.marker, identity=shown))
    steps = []
    if error.running is None:
        lines.append("Whether application containers run is unknown.")
        status = " ".join(status_command(host))
        steps.append(f"make sure {status} lists no application service")
    elif error.running:
        names = sorted(error.running)
        lines.append(f"Application containers still run: {', '.join(names)}.")
        steps.append(f"stop them with {' '.join(compose(host))} stop -t 660 {' '.join(names)}")
    else:
        lines.append("No application container runs: the site is down.")
    if error.marker != "held":
        steps.append(f"write the identity back with printf %s {shown} > {host.marker} && sync")
    steps.append(f"run {host.command(rerun)} again")
    lines.append("Next: " + "; then ".join(steps) + ".")
    print_safely("\n".join(lines), error=True)


def release_broken_streams() -> None:
    # CPython exits with 120 instead of main's status when it cannot flush stdout or stderr at exit.
    for stream in (sys.stdout, sys.stderr):
        with suppress(ValueError):  # closed, which the exit skips, or not backed by a descriptor
            try:
                stream.flush()
            except OSError:
                descriptor = stream.fileno()
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, descriptor)
                os.close(devnull)


def main(arguments: list[str] | None = None) -> int:
    try:
        return command_line(arguments)
    finally:
        release_broken_streams()


def command_line(arguments: list[str] | None) -> int:
    parser = argparse.ArgumentParser(
        description="Reset or restore the production database during a planned maintenance."
    )
    parser.add_argument("host_config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare", help="stop the site and keep a verified pre-reset archive")
    commands.add_parser("abort", help="start the previous release again")
    for name, text in (
        ("reset", "purge the queues and recreate an empty database"),
        ("restore", "bring the database back from a verified archive and start the release"),
    ):
        commands.add_parser(name, help=text).add_argument(
            "--confirm", metavar="OBJECT", required=True
        )
    options = parser.parse_args(arguments)
    try:
        host = load_host(options.host_config)
    except (OSError, ValueError, KeyError) as error:
        print(
            f"Cannot read the host configuration {options.host_config}: {error!r}", file=sys.stderr
        )
        return 2
    try:
        match options.command:
            case "prepare":
                prepare(host)
            case "abort":
                abort(host)
            case "reset":
                reset(host, options.confirm)
            case _:
                restore(host, options.confirm)
    except RefusedError as error:
        print(f"Refused: {error} No data was changed.", file=sys.stderr)
        return 2
    except StartFailedError as error:
        try:
            print_failed_start(host, options, error)
        finally:
            restore_signals(error.handlers, error.signals)
        return 130 if error.interrupted else 1
    except KeyboardInterrupt:
        print(f"{options.command} was interrupted.", file=sys.stderr)
        print_recovery(host, options)
        return 130
    except Exception as error:
        print(outcome(options.command, error), file=sys.stderr)
        print_recovery(host, options)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
