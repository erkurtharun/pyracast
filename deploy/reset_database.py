# ruff: noqa: S603, S607 -- Fixed Docker, curl, sync and systemctl executables with argument lists.
import argparse
import base64
import dataclasses
import datetime
import hashlib
import importlib
import json
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy.backup import compose_command, upload_backup
from deploy.operations_log import access_token

# The installer's drain order: ingress first, then the producer, then the consumers.
STOP_ORDER = (
    ("100", ("gateway", "api", "web")),
    ("660", ("dispatcher",)),
    ("660", ("worker", "notifications")),
)
APPLICATION_SERVICES = frozenset(service for _, group in STOP_ORDER for service in group)
SUPPORT_SERVICES = ("postgres", "rabbitmq")
OBJECT_PATTERN = re.compile(r"postgres/[A-Za-z0-9/_.-]+\.dump")
BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")
# Roles and their memberships belong to the cluster and survive DROP DATABASE.
ROLE_STATEMENTS = ("CREATE ROLE ", "GRANT pyracast_owner TO ")
COUNT_SQL = (
    "SELECT c.relname || ' ' || (xpath('/row/n/text()', query_to_xml("
    "format('SELECT count(*) AS n FROM public.%I', c.relname), false, true, '')))[1]::text "
    "FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace "
    "WHERE s.nspname = 'public' AND c.relkind = 'r' ORDER BY c.relname"
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
             AND NOT has_table_privilege('pyracast_app', format('public.%I', tablename),
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
    def replaced(self) -> Path:
        """Exists from just before DROP DATABASE until a restore has passed its checks."""
        return self.state / "database-replaced"

    def command(self, arguments: str) -> str:
        return f"python3 {self.release}/deploy/reset_database.py {self.config} {arguments}"


@dataclasses.dataclass(frozen=True)
class Record:
    object: str
    sha256: str
    counts: dict[str, int]


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


def save_record(host: Host, record: Record) -> None:
    path = record_path(host, record.object)
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(dataclasses.asdict(record), indent=2, sort_keys=True))
    temporary.replace(path)


def load_record(host: Host, name: str) -> Record | None:
    path = record_path(host, name)
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    counts = {str(table): int(count) for table, count in data["counts"].items()}
    return Record(object=str(data["object"]), sha256=str(data["sha256"]), counts=counts)


def require_object_name(name: str) -> None:
    if not OBJECT_PATTERN.fullmatch(name):
        raise RefusedError(f"{name!r} is not a PostgreSQL archive object name (postgres/....dump).")


def verified_record(host: Host, name: str) -> tuple[Record, Path]:
    record = load_record(host, name)
    if record is None:
        raise RefusedError(
            f"No verified archive {name} on this host. "
            "Name the object exactly as prepare printed it."
        )
    archive = archive_path(host, name)
    if not archive.is_file():
        raise RefusedError(
            f"The verified archive {archive} is missing; copy it back to that path first."
        )
    if file_sha256(archive) != record.sha256:
        raise RefusedError(f"{archive} no longer matches its recorded SHA-256 {record.sha256}.")
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


def sync(path: Path) -> None:
    subprocess.run(["sync", "-f", str(path)], check=True)


def block_release(host: Host) -> None:
    host.marker.parent.mkdir(parents=True, exist_ok=True)
    host.marker.touch()
    sync(host.marker)
    sync(host.marker.parent)


def running_services(host: Host) -> set[str]:
    return set(capture([*compose(host), "ps", "--services", "--status", "running"]).split())


def stop_application(host: Host) -> None:
    for timeout, services in STOP_ORDER:
        step(f"Stopping {', '.join(services)}")
        subprocess.run(
            [*compose(host), "stop", "-t", timeout, *services],
            check=True,
            timeout=int(timeout) + 60,
        )
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
    name = f"postgres/{stamp}-prereset-{uuid.uuid4().hex}.dump"
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


def verify_archive(host: Host, archive: Path) -> dict[str, int]:
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
        return parse_counts(capture([*query, "-At", "-c", COUNT_SQL]))
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=False)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True, check=False)


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


def fetch_record(host: Host, name: str) -> None:
    step(f"Downloading gs://{host.bucket}/{name}")
    archive = download_archive(host, name)
    step("Restoring it into a throwaway PostgreSQL")
    counts = verify_archive(host, archive)
    save_record(host, Record(object=name, sha256=file_sha256(archive), counts=counts))


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


def recreate_database(host: Host, name: str) -> None:
    host.replaced.write_text(name)
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


def start_release(host: Host) -> None:
    rearm = not (host.environment / "runtime.env").is_file()
    if rearm:
        step("Writing the runtime secrets again (the host restarted during maintenance)")
        subprocess.run(
            [sys.executable, str(host.release / "deploy/runtime_secrets.py"), str(host.config)],
            check=True,
        )
    step(f"Starting the release in {host.release}")
    subprocess.run(
        [*compose(host), "up", "--detach", "--wait", "--wait-timeout", "180"],
        check=True,
        timeout=300,
    )
    if not site_ready(host, attempts=18):
        raise RuntimeError("The release started, but /health/ready/ did not answer ok.")
    host.marker.unlink(missing_ok=True)
    sync(host.marker.parent)
    if rearm:
        subprocess.run(["systemctl", "start", "pyracast.service"], check=True)


def print_prepared(host: Host, record: Record) -> None:
    print("\nPre-reset archive verified: the restored copy has every table's live row count.")
    print(f"  Object:  gs://{host.bucket}/{record.object}")
    print(f"  SHA-256: {record.sha256}")
    print(f"  Archive: {archive_path(host, record.object)}")
    print(f"  Release commit: {host.release.resolve().name}")
    for key, value in sorted(release_values(host).items()):
        if key.endswith("_IMAGE"):
            print(f"  {key}={value}")
    print(f"  Runtime secret version: {host.runtime_version}")
    print(f"  Tables: {len(record.counts)}, rows: {sum(record.counts.values())}")
    for table, count in sorted(record.counts.items()):
        print(f"    {table} {count}")
    print("The site is down. Copy the archive off the host, then run:")
    print(f"  {host.command(f'reset --confirm {record.object}')}")
    print("To serve the previous release again instead, run:")
    print(f"  {host.command('abort')}")


def prepare(host: Host) -> None:
    with release_lock(host):
        if host.marker.exists():
            raise RefusedError(f"{host.marker} exists: a release or reset is in progress.")
        if not site_ready(host, attempts=0):
            raise RefusedError("The site does not answer /health/ready/ with ok.")
        host.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        host.replaced.unlink(missing_ok=True)
        step(f"Blocking automatic starts with {host.marker}")
        block_release(host)
        stop_application(host)
        step("Dumping database pyracast")
        name, archive = dump_database(host)
        digest = file_sha256(archive)
        step(f"Uploading gs://{host.bucket}/{name}")
        upload_backup(host.bucket, name, archive)
        live = table_counts(host)
        step("Restoring the archive into a throwaway PostgreSQL")
        require_equal_counts(live, verify_archive(host, archive), "Live and restored")
        record = Record(object=name, sha256=digest, counts=live)
        save_record(host, record)
    print_prepared(host, record)


def abort(host: Host) -> None:
    with release_lock(host):
        if host.replaced.exists():
            name = host.replaced.read_text()
            raise RefusedError(
                "The database was replaced after prepare; starting the previous release now would "
                f"serve it without its data. Run {host.command(f'restore --confirm {name}')}"
            )
        start_release(host)
    print("The previous release serves again and the marker is removed. Nothing else to do.")


def reset(host: Host, name: str) -> None:
    require_object_name(name)
    with release_lock(host):
        require_maintenance(host)
        verified_record(host, name)
        start_support(host)
        require_roles(host)
        purge_queues(host)
        recreate_database(host, name)
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
        host.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if load_record(host, name) is None:
            fetch_record(host, name)
        record, archive = verified_record(host, name)
        start_support(host)
        require_roles(host)
        purge_queues(host)
        recreate_database(host, name)
        restore_live(host, archive)
        require_equal_counts(record.counts, table_counts(host), "Archived and restored")
        check_roles(host)
        host.replaced.unlink()
        start_release(host)
    print(f"\n{name} is restored and the previous release serves again; the marker is removed.")


RECOVERY = {
    "prepare": "If the marker exists the site is down: run {abort} to serve the previous release "
    "again, then run prepare again.",
    "abort": "The marker stays and the site is down. Inspect the containers and run {abort} again.",
    "reset": "The marker stays and the site is down. Every step repeats safely: run the same reset "
    "command again, or bring the data back with {restore}.",
    "restore": "The marker stays and the site is down. Every step repeats safely from the verified "
    "archive: run {restore} again.",
}


def main(arguments: list[str] | None = None) -> int:
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
    except Exception as error:
        print(f"{options.command} failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            detail = error.stderr if isinstance(error.stderr, str) else error.stderr.decode()
            print(detail.strip()[-2000:], file=sys.stderr)
        confirm = getattr(options, "confirm", "OBJECT")
        print(
            RECOVERY[options.command].format(
                abort=host.command("abort"), restore=host.command(f"restore --confirm {confirm}")
            ),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
