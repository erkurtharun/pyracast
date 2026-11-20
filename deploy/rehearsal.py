import argparse
import dataclasses
import json
import re
import shutil
import signal
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from subprocess import DEVNULL, CalledProcessError, TimeoutExpired

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import backup, host, reset_database
from deploy.operations_log import report

LOCK = Path("/var/lib/pyracast/rehearsal.lock")
MEMINFO = Path("/proc/meminfo")
LABEL = "pyracast.rehearsal"
DEADLINE_SECONDS = 3600
DATABASE_MEMORY_MB = 384
PROBE_MEMORY_MB = 256
MEMORY_RESERVE_MB = 256
# Room for the download, and on Docker's disk for a restored database of up to three times the
# compressed archive besides the archive itself.
DISK_FACTOR = 4
SECRETS = re.compile(r"^\S+: (\d+) primary, (\d+) fallback, \d+ unreadable$", re.M)
# The probe shares the throwaway's network namespace, which has no route to the live database.
OVERRIDE = """services:
  api:
    network_mode: container:{container}
    networks: !reset []
    environment:
      PGHOST: 127.0.0.1
      PGPORT: "5432"
    mem_limit: {memory}m
    cpus: 0.5
"""


class StepFailedError(Exception):
    def __init__(self, step: str, error: BaseException) -> None:
        super().__init__(step)
        self.step = step
        self.error = type(error).__name__


@contextmanager
def stage(name: str) -> Iterator[None]:
    try:
        yield
    except Exception as error:
        raise StepFailedError(name, error) from error


@dataclasses.dataclass(frozen=True)
class Pointer:
    object: str
    kind: str
    size: int
    counts: dict[str, int]


def load_pointer() -> Pointer:
    data = json.loads(host.LAST_BACKUP.read_text())
    name = str(data["object"])
    if not reset_database.OBJECT_PATTERN.fullmatch(name) or data["kind"] not in (
        "hourly",
        "prerelease",
    ):
        raise ValueError("The backup pointer names no archive to rehearse.")
    counts = {str(table): int(count) for table, count in data["counts"].items()}
    return Pointer(object=name, kind=str(data["kind"]), size=int(data["size"]), counts=counts)


class Throwaway:
    def __init__(self, helper: reset_database.Host) -> None:
        self.helper = helper
        self.expires = time.monotonic() + DEADLINE_SECONDS
        self.container = f"pyracast-rehearsal-{uuid.uuid4().hex}"
        self.volume = f"{self.container}-data"
        self.docker_root = Path("/var/lib/docker")

    def remaining(self) -> float:
        left = self.expires - time.monotonic()
        if left <= 0:
            raise TimeoutExpired(["rehearsal"], DEADLINE_SECONDS)
        return left

    def capture(self, command: list[str], script: str = "") -> str:
        return reset_database.capture(command, script=script, timeout=max(1, int(self.remaining())))

    def psql(self, *arguments: str, script: str = "") -> str:
        connection = ["-h", "127.0.0.1", "-U", "postgres", "-d", "pyracast"]
        command = ["docker", "exec", "-i", self.container, "psql", "-X", "-v", "ON_ERROR_STOP=1"]
        return self.capture([*command, *connection, *arguments], script)


def sweep() -> None:
    containers = reset_database.capture(
        ["docker", "ps", "--all", "--quiet", "--filter", f"label={LABEL}"], timeout=60
    ).split()
    if containers:
        reset_database.capture(["docker", "rm", "--force", *containers], timeout=120)
    volumes = reset_database.capture(
        ["docker", "volume", "ls", "--quiet", "--filter", f"label={LABEL}"], timeout=60
    ).split()
    if volumes:
        reset_database.capture(["docker", "volume", "rm", *volumes], timeout=120)


def available_memory_mb() -> float:
    memory = dict(line.split(":", 1) for line in MEMINFO.read_text().splitlines())
    return int(memory["MemAvailable"].split()[0]) / 1024


def require_room(path: Path, needed: int) -> None:
    usage = shutil.disk_usage(path)
    if usage.free - usage.total * backup.FREE_FLOOR_FRACTION < needed:
        raise backup.CapacityError(f"{path} lacks {needed} bytes above its free-space floor.")


def require_capacity(throwaway: Throwaway, pointer: Pointer, directory: Path) -> None:
    needed = DATABASE_MEMORY_MB + PROBE_MEMORY_MB + MEMORY_RESERVE_MB
    if available_memory_mb() < needed:
        raise backup.CapacityError(f"Less than {needed} MB of memory is available.")
    info = throwaway.capture(["docker", "info", "--format", "{{.DockerRootDir}}"])
    throwaway.docker_root = Path(info.strip())
    require_room(directory, pointer.size)
    require_room(throwaway.docker_root, DISK_FACTOR * pointer.size)


def start(throwaway: Throwaway) -> None:
    configuration = throwaway.capture(
        [*reset_database.compose(throwaway.helper), "config", "--format", "json"]
    )
    image = json.loads(configuration)["services"]["postgres"]["image"]
    memory = f"{DATABASE_MEMORY_MB}m"
    throwaway.capture(["docker", "volume", "create", "--label", LABEL, throwaway.volume])
    throwaway.capture(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            throwaway.container,
            "--label",
            LABEL,
            "--network",
            "none",
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--cpus",
            "0.5",
            "--pids-limit",
            "100",
            "--security-opt",
            "no-new-privileges:true",
            "--env",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "--env",
            "POSTGRES_DB=pyracast",
            "--volume",
            f"{throwaway.volume}:/var/lib/postgresql",
            image,
            "postgres",
            "-c",
            "shared_buffers=64MB",
            "-c",
            "max_connections=10",
            "-c",
            "maintenance_work_mem=64MB",
            "-c",
            "fsync=off",
            "-c",
            "full_page_writes=off",
        ]
    )
    reset_database.wait_until_ready(throwaway.container)


def restore(throwaway: Throwaway, archive: Path) -> None:
    # As the recovery helper restores the live database: roles first, then owners and privileges.
    bootstrap = (throwaway.helper.release / "deploy/bootstrap.sql").read_text()
    throwaway.psql("--single-transaction", script=bootstrap)
    with archive.open("rb") as stream:
        backup.bounded_run(
            [
                "docker",
                "exec",
                "-i",
                throwaway.container,
                "pg_restore",
                "-h",
                "127.0.0.1",
                "-U",
                "postgres",
                "-d",
                "pyracast",
                "--exit-on-error",
                "--single-transaction",
            ],
            stdin=stream,
            stdout=DEVNULL,
            stderr=None,
            timeout=throwaway.remaining(),
            disk=throwaway.docker_root,
        )
    throwaway.psql("--single-transaction", script=bootstrap)


def probe(throwaway: Throwaway, directory: Path, *command: str) -> str:
    override = directory / "rehearsal.yaml"
    override.write_text(OVERRIDE.format(container=throwaway.container, memory=PROBE_MEMORY_MB))
    run = ["-f", str(override), "run", "--rm", "--no-deps", "api", "pyracast", *command]
    return throwaway.capture([*reset_database.compose(throwaway.helper), *run])


def decrypted(output: str) -> int:
    count = sum(int(primary) + int(fallback) for primary, fallback in SECRETS.findall(output))
    if not count:
        raise RuntimeError("No stored secret decrypted with the release's keys.")
    return count


def remove(throwaway: Throwaway) -> None:
    for command in (
        ["docker", "rm", "--force", throwaway.container],
        ["docker", "volume", "rm", throwaway.volume],
    ):
        with suppress(CalledProcessError, TimeoutExpired, OSError):
            reset_database.capture(command, timeout=120)


def rehearse(helper: reset_database.Host, directory: Path) -> dict[str, object]:
    with stage("pointer"):
        pointer = load_pointer()
    throwaway = Throwaway(helper)
    with stage("sweep"):
        sweep()
    with stage("capacity"):
        require_capacity(throwaway, pointer, directory)
    started = time.monotonic()
    try:
        with stage("download"):
            reset_database.step(f"Downloading gs://{helper.bucket}/{pointer.object}")
            downloads = dataclasses.replace(helper, state=directory)
            archive = reset_database.download_archive(downloads, pointer.object)
        with stage("restore"):
            reset_database.step("Restoring it with its roles into a throwaway PostgreSQL")
            start(throwaway)
            restore(throwaway, archive)
        with stage("roles"):
            throwaway.psql(script=reset_database.ROLE_CHECK_SQL)
        with stage("counts"):
            counts = reset_database.parse_counts(
                throwaway.psql("-At", "-c", reset_database.COUNT_SQL)
            )
            reset_database.require_equal_counts(pointer.counts, counts, "Dumped and restored")
        recovery = round(time.monotonic() - started, 1)
        with stage("migrations"):
            check = "--check" if pointer.kind == "hourly" else "--plan"
            probe(throwaway, directory, "migrate", check)
        with stage("secrets"):
            secrets = decrypted(probe(throwaway, directory, "check_secrets"))
    finally:
        remove(throwaway)
    return {
        "object": pointer.object,
        "recovery_seconds": recovery,
        "tables": len(counts),
        "rows": sum(counts.values()),
        "secrets": secrets,
    }


def record(status: dict[str, object]) -> None:
    backup.write_atomically(host.LAST_REHEARSAL, json.dumps({**status, "mark": host.clock_mark()}))


def terminate(_signal: int, _frame: object) -> None:
    raise SystemExit(143)


def run_rehearsal(helper: reset_database.Host) -> int:
    with tempfile.TemporaryDirectory(prefix="pyracast-rehearsal-") as temporary:
        try:
            passed = rehearse(helper, Path(temporary))
        except StepFailedError as failure:
            record({"status": "failed", "step": failure.step})
            report("pyracast_rehearsal_failed", "WARNING", step=failure.step, error=failure.error)
            print(f"The rehearsal failed in step {failure.step}: {failure.error}.", file=sys.stderr)
            return 1
        except SystemExit:
            record({"status": "failed", "step": "terminated"})
            report("pyracast_rehearsal_failed", "WARNING", step="terminated", error="SystemExit")
            raise
    record({"status": "passed", **passed})
    report("pyracast_rehearsal_passed", "INFO", **passed)
    return 0


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore the latest backup into a throwaway PostgreSQL beside the running "
        "site, prove that it serves and record the outcome."
    )
    parser.add_argument("host_config", type=Path)
    options = parser.parse_args(arguments)
    helper = reset_database.load_host(options.host_config)
    # systemd stops a unit with SIGTERM; unwinding removes the throwaway database.
    signal.signal(signal.SIGTERM, terminate)
    with host.open_lock(LOCK) as lock:
        if not host.try_lock(lock):
            print("Another restore rehearsal holds its lock; nothing was changed.", file=sys.stderr)
            return 2
        return run_rehearsal(helper)


if __name__ == "__main__":
    raise SystemExit(main())
