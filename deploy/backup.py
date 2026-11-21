# ruff: noqa: S603 -- Fixed Docker executables with argument lists; no shell interpolation.
import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPException
from pathlib import Path
from typing import IO, Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import host
from deploy.operations_log import access_token, report

HOURLY_LOCK_WAIT_SECONDS = 600
DUMP_TIMEOUT_SECONDS = 1200
SKIPPED_EXIT_STATUS = 75
REFUSED_EXIT_STATUS = 2
# A dump or restore stops before its file system has less than this share free; the watchdog
# warns at 15 percent.
FREE_FLOOR_FRACTION = 0.05
DAILY_PREFIX = "daily/"
# Within one location and storage class Cloud Storage finishes a rewrite in its first call.
REWRITE_CALLS = 10
COUNTS_LINE = "pyracast-counts "
# One REPEATABLE READ transaction counts every table and exports its snapshot to pg_dump, so the
# counts describe exactly the rows the archive holds. psql -q prints no command tags, and \gset
# and \warn print nothing to stdout, which carries only the archive.
DUMP_SCRIPT = (
    "BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY;\n"
    "SELECT pg_export_snapshot() AS snapshot \\gset\n"
    "SELECT coalesce(json_object_agg(c.relname, (xpath('/row/n/text()', query_to_xml("
    "format('SELECT count(*) AS n FROM public.%I', c.relname), false, true, '')))[1]"
    "::text::bigint ORDER BY c.relname), '{}') AS counts "
    "FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace "
    "WHERE s.nspname = 'public' AND c.relkind = 'r' \\gset\n"
    "\\warn pyracast-counts :counts\n"
    "\\setenv PYRACAST_SNAPSHOT :snapshot\n"
    "\\! pg_dump -U postgres -d pyracast --format=custom --compress=zstd:3 "
    '--snapshot="$PYRACAST_SNAPSHOT"\n'
    "\\if :SHELL_ERROR\n"
    "DO $$ BEGIN RAISE EXCEPTION 'pg_dump failed'; END $$;\n"
    "\\endif\n"
    "COMMIT;\n"
)


class ReleaseLockNotHeldError(Exception):
    pass


class CapacityError(RuntimeError):
    pass


def compose_command(release: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(release / "deploy/release.env"),
        "-f",
        str(release / "deploy/compose.yaml"),
    ]


def retryable(status: int) -> bool:
    return status in (408, 429) or status >= 500


@contextmanager
def storage_response(request: urllib.request.Request, timeout: float) -> Iterator[IO[bytes]]:
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        yield response


def storage_json(request: urllib.request.Request, timeout: float) -> Any:
    with storage_response(request, timeout) as response:
        return json.load(response)


def object_metadata(bucket: str, name: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(name, safe='')}",
        headers={"Authorization": f"Bearer {access_token()}"},
    )
    result = storage_json(request, 30)
    return result if isinstance(result, dict) else {}


def matches(result: dict[str, Any], checksum: str, size: int) -> bool:
    return result.get("md5Hash") == checksum and result.get("size") == str(size)


def upload_backup(bucket: str, name: str, archive: Path) -> None:
    query = urlencode({"uploadType": "media", "name": name, "ifGenerationMatch": "0"})
    url = f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?{query}"
    with archive.open("rb") as stream:
        expected = base64.b64encode(hashlib.file_digest(stream, "md5").digest()).decode()
    size = archive.stat().st_size
    attempt = 0
    while True:
        try:
            try:
                with archive.open("rb") as stream:
                    request = urllib.request.Request(
                        url,
                        data=stream,
                        method="POST",
                        headers={
                            "Authorization": f"Bearer {access_token()}",
                            "Content-Type": "application/octet-stream",
                            "Content-Length": str(size),
                            "X-Goog-Hash": f"md5={expected}",
                        },
                    )
                    result = storage_json(request, 180)
            except HTTPError as error:
                error.close()
                if error.code != 412:
                    raise
                if not matches(object_metadata(bucket, name), expected, size):
                    raise ValueError("Existing backup checksum does not match.") from None
                return
            if not isinstance(result, dict) or not matches(result, expected, size):
                raise ValueError("Uploaded backup checksum does not match.")
            return
        except HTTPError as error:
            error.close()
            if attempt == 2 or not retryable(error.code):
                raise
        except (OSError, ValueError, HTTPException):
            if attempt == 2:
                raise
        time.sleep(2**attempt)
        attempt += 1


def wait_within(
    process: subprocess.Popen[bytes],
    command: list[str],
    timeout: float,
    deadline: float,
    disk: Path,
    floor: float,
) -> int:
    while True:
        try:
            return process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if shutil.disk_usage(disk).free < floor:
                raise CapacityError(f"{disk} fell below {FREE_FLOOR_FRACTION:.0%} free.") from None
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout) from None


def bounded_run(
    command: list[str],
    *,
    stdin: IO[bytes] | None,
    stdout: IO[bytes] | int | None,
    stderr: IO[bytes] | int | None,
    timeout: float,
    disk: Path,
) -> None:
    deadline = time.monotonic() + timeout
    usage = shutil.disk_usage(disk)
    floor = usage.total * FREE_FLOOR_FRACTION
    if usage.free < floor:
        raise CapacityError(f"{disk} has less than {FREE_FLOOR_FRACTION:.0%} free.")
    with subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=stderr) as process:
        try:
            status = wait_within(process, command, timeout, deadline, disk, floor)
        except BaseException:
            # Popen.__exit__ kills only on KeyboardInterrupt and otherwise waits for the child.
            process.kill()
            raise
    if status:
        raise subprocess.CalledProcessError(status, command)


def dumped_counts(errors: str) -> dict[str, int]:
    lines = [line for line in errors.splitlines() if line.startswith(COUNTS_LINE)]
    try:
        counts = json.loads(lines[0].removeprefix(COUNTS_LINE))
    except (IndexError, ValueError):
        counts = None
    if not isinstance(counts, dict):
        raise ValueError("The dump reported no table counts.")
    return {str(table): int(count) for table, count in counts.items()}


def dump_database(release: Path, archive: Path) -> dict[str, int]:
    compose = compose_command(release)
    session = [
        *compose,
        "exec",
        "-T",
        "--env",
        "PGAPPNAME=pyracast-backup",
        "postgres",
        "psql",
        "-X",
        "-q",
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        "-d",
        "pyracast",
    ]
    with (
        tempfile.TemporaryFile() as script,
        tempfile.TemporaryFile() as errors,
        archive.open("wb") as stream,
    ):
        script.write(DUMP_SCRIPT.encode())
        script.seek(0)
        try:
            bounded_run(
                session,
                stdin=script,
                stdout=stream,
                stderr=errors,
                timeout=DUMP_TIMEOUT_SECONDS,
                disk=archive.parent,
            )
        except subprocess.CalledProcessError:
            raise
        except BaseException:
            # Killing the Compose client leaves pg_dump and its snapshot running in the container.
            subprocess.run(
                [
                    *compose,
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "pyracast",
                    "-c",
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE application_name = 'pyracast-backup'",
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
            raise
        finally:
            errors.seek(0)
            output = errors.read().decode(errors="replace")
            journal = [line for line in output.splitlines() if not line.startswith(COUNTS_LINE)]
            if journal:
                print("\n".join(journal), file=sys.stderr, flush=True)
    return dumped_counts(output)


def write_atomically(path: Path, content: str) -> None:
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}")
    with temporary.open("w") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def store_backup(release: Path, bucket: str, kind: str, directory: Path) -> tuple[str, Path]:
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y/%m/%d/%Y%m%dT%H%M%SZ")
    name = f"postgres/{stamp}-{kind}-{uuid.uuid4().hex}.dump"
    archive = directory / name.rsplit("/", 1)[-1]
    partial = archive.with_suffix(".partial")
    try:
        counts = dump_database(release, partial)
        partial.replace(archive)
        upload_backup(bucket, name, archive)
        if kind == "prerelease":
            record = {"object": name, "counts": counts}
            write_atomically(release / host.PRERELEASE_RECORD, json.dumps(record))
        pointer = {
            "object": name,
            "kind": kind,
            "size": archive.stat().st_size,
            "counts": counts,
            "mark": host.clock_mark(),
        }
        write_atomically(host.LAST_BACKUP, json.dumps(pointer))
    except BaseException:
        partial.unlink(missing_ok=True)
        archive.unlink(missing_ok=True)
        raise
    return name, archive


def copy_daily(bucket: str, name: str) -> str | None:
    target = DAILY_PREFIX + datetime.datetime.now(datetime.UTC).strftime("%Y/%m/%d") + ".dump"
    path = f"{bucket}/o/{quote(name, safe='')}/rewriteTo/b/{bucket}/o/{quote(target, safe='')}"
    query = {"ifGenerationMatch": "0"}
    for _call in range(REWRITE_CALLS):
        request = urllib.request.Request(
            f"https://storage.googleapis.com/storage/v1/b/{path}?{urlencode(query)}",
            data=b"{}",
            method="POST",
            headers={
                "Authorization": f"Bearer {access_token()}",
                "Content-Type": "application/json",
            },
        )
        try:
            result = storage_json(request, 180)
        except HTTPError as error:
            error.close()
            if error.code == 412:
                return None
            raise
        if result.get("done"):
            return target
        query["rewriteToken"] = str(result["rewriteToken"])
    raise RuntimeError("The daily copy did not finish.")


def keep_daily(bucket: str, name: str) -> None:
    try:
        copied = copy_daily(bucket, name)
    except Exception as error:
        report("pyracast_backup_daily_copy_failed", "WARNING", error=type(error).__name__)
        return
    if copied is not None:
        report("pyracast_backup_daily_copied", "INFO", object=copied)


def create_backup(
    release: Path,
    bucket: str,
    kind: str,
    directory: Path,
    lock_descriptor: int | None = None,
    *,
    daily: bool = False,
) -> str | None:
    started = time.monotonic()
    if kind == "prerelease":
        # The installer holds the release lock and passes it; a second request would wait forever.
        if lock_descriptor is None or not host.holds_release_lock(lock_descriptor):
            raise ReleaseLockNotHeldError
        # The recovery helper restores this copy without the bucket until a release succeeds.
        host.RECOVERY_STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        name, archive = store_backup(release, bucket, kind, host.RECOVERY_STATE)
    else:
        # Dump, upload and pointer stay under one lock, so the pointer always names the newest dump.
        with host.release_lock(HOURLY_LOCK_WAIT_SECONDS) as locked:
            if not locked:
                report("pyracast_backup_skipped", "WARNING", reason="lock_timeout")
                return None
            if host.RELEASE_BLOCKED.exists():
                report("pyracast_backup_skipped", "WARNING", reason="release_blocked")
                return None
            name, archive = store_backup(release, bucket, kind, directory)
    report(
        "pyracast_backup_succeeded",
        "INFO",
        object=name,
        size=archive.stat().st_size,
        duration_seconds=round(time.monotonic() - started, 1),
    )
    if daily and kind == "hourly":
        keep_daily(bucket, name)
    return name


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("host_config", type=Path)
    parser.add_argument("--release-dir", type=Path, default=host.CURRENT_RELEASE)
    parser.add_argument("--kind", choices=("hourly", "prerelease"), default="hourly")
    parser.add_argument("--lock-fd", type=int)
    options = parser.parse_args(arguments)
    config = json.loads(options.host_config.read_text())
    bucket = config["backup_bucket"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", bucket):
        raise ValueError("Invalid backup bucket.")
    # Read outside host_contract.load(), so a bad unrelated setting never stops hourly backups.
    daily = config.get("daily_backups", False)
    with tempfile.TemporaryDirectory(prefix="pyracast-backup-") as temporary:
        try:
            name = create_backup(
                options.release_dir,
                bucket,
                options.kind,
                Path(temporary),
                options.lock_fd,
                daily=daily is True,
            )
        except ReleaseLockNotHeldError:
            print(
                "Refused: a prerelease backup runs only inside a release, which passes the "
                "release lock it holds with --lock-fd. Nothing was dumped.",
                file=sys.stderr,
            )
            return REFUSED_EXIT_STATUS
        if not isinstance(daily, bool):
            report("pyracast_backup_setting_invalid", "ERROR", setting="daily_backups")
            print(
                f"The daily_backups setting of {options.host_config} is neither true nor false; "
                "no daily copy was made.",
                file=sys.stderr,
            )
            return 1
        if name is None:
            return SKIPPED_EXIT_STATUS
    return 0


def terminate(_signal: int, _frame: object) -> None:
    raise SystemExit(143)


def run(arguments: list[str] | None = None) -> int:
    # systemd stops a unit with SIGTERM; unwinding runs the temporary directory cleanup.
    signal.signal(signal.SIGTERM, terminate)
    try:
        return main(arguments)
    except SystemExit as stopped:
        if stopped.code == 143:
            report("pyracast_backup_failed", "ERROR", reason="terminated")
        raise
    except Exception:
        report("pyracast_backup_failed", "ERROR")
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
