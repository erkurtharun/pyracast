# ruff: noqa: S603, S607 -- Fixed Docker executables with argument lists; no shell interpolation.
import argparse
import base64
import datetime
import hashlib
import json
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import host
from deploy.operations_log import access_token, report

HOURLY_LOCK_WAIT_SECONDS = 600
DUMP_TIMEOUT_SECONDS = 1200
SKIPPED_EXIT_STATUS = 75


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


def object_metadata(bucket: str, name: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(name, safe='')}",
        headers={"Authorization": f"Bearer {access_token()}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        result = json.load(response)
    return result if isinstance(result, dict) else {}


def matches(result: dict[str, Any], checksum: str, size: int) -> bool:
    return result.get("md5Hash") == checksum and result.get("size") == str(size)


def upload_backup(bucket: str, name: str, archive: Path) -> None:
    query = urlencode({"uploadType": "media", "name": name, "ifGenerationMatch": "0"})
    url = f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?{query}"
    with archive.open("rb") as stream:
        expected = base64.b64encode(hashlib.file_digest(stream, "md5").digest()).decode()
    size = archive.stat().st_size
    for attempt in range(3):
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
                    with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310
                        result = json.load(response)
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


def dump_database(release: Path, archive: Path) -> None:
    compose = compose_command(release)
    with archive.open("wb") as stream:
        try:
            subprocess.run(
                [
                    *compose,
                    "exec",
                    "-T",
                    "--env",
                    "PGAPPNAME=pyracast-backup",
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
                timeout=DUMP_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
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


def locked_dump(release: Path, archive: Path) -> bool:
    with host.release_lock(HOURLY_LOCK_WAIT_SECONDS) as locked:
        if not locked:
            report("pyracast_backup_skipped", "WARNING", reason="lock_timeout")
            return False
        if host.RELEASE_BLOCKED.exists():
            report("pyracast_backup_skipped", "WARNING", reason="release_blocked")
            return False
        dump_database(release, archive)
    return True


def create_backup(release: Path, bucket: str, kind: str, directory: Path) -> str | None:
    started = time.monotonic()
    archive = directory / "database.dump"
    if kind == "prerelease":
        # The installer already holds the release lock; a second lock request would wait forever.
        dump_database(release, archive)
    elif not locked_dump(release, archive):
        return None
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y/%m/%d/%Y%m%dT%H%M%SZ")
    name = f"postgres/{stamp}-{kind}-{uuid.uuid4().hex}.dump"
    upload_backup(bucket, name, archive)
    # Hourly and pre-release backups can finish at the same time; each needs its own file.
    temporary = host.LAST_BACKUP.with_name(f"{host.LAST_BACKUP.name}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps({"object": name, "completed_at": time.time()}))
    temporary.replace(host.LAST_BACKUP)
    report(
        "pyracast_backup_succeeded",
        "INFO",
        object=name,
        size=archive.stat().st_size,
        duration_seconds=round(time.monotonic() - started, 1),
    )
    return name


def download_backup(bucket: str, name: str, archive: Path) -> None:
    expected = object_metadata(bucket, name)
    request = urllib.request.Request(
        f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(name, safe='')}?alt=media",
        headers={"Authorization": f"Bearer {access_token()}"},
    )
    digest = hashlib.md5(usedforsecurity=False)
    with urllib.request.urlopen(request, timeout=300) as response, archive.open("wb") as stream:  # noqa: S310
        while block := response.read(1024 * 1024):
            digest.update(block)
            stream.write(block)
    checksum = base64.b64encode(digest.digest()).decode()
    if not matches(expected, checksum, archive.stat().st_size):
        raise ValueError("Downloaded backup does not match its recorded checksum.")


def rehearse_restore(release: Path, bucket: str, name: str, directory: Path) -> None:
    if not re.fullmatch(r"postgres/[A-Za-z0-9/_.-]+\.dump", name):
        raise ValueError("Select a PostgreSQL archive object from the configured backup bucket.")
    archive = directory / "restore.dump"
    download_backup(bucket, name, archive)
    restore_archive(release, archive, name)


def restore_archive(release: Path, archive: Path, name: str) -> None:
    configuration = json.loads(
        subprocess.check_output(
            [*compose_command(release), "config", "--format", "json"],
            text=True,
        )
    )
    image = configuration["services"]["postgres"]["image"]
    container = "pyracast-restore-" + uuid.uuid4().hex
    volume = container + "-data"
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
        for attempt in range(60):
            ready = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "postgres",
                    "-d",
                    "pyracast",
                ],
                capture_output=True,
            )
            if ready.returncode == 0:
                break
            if attempt == 59:
                raise RuntimeError("Restore database did not become ready.")
            time.sleep(1)
        with archive.open("rb") as stream:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "pg_restore",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "postgres",
                    "-d",
                    "pyracast",
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    "--no-acl",
                ],
                stdin=stream,
                check=True,
                timeout=1200,
            )
        subprocess.run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-h",
                "127.0.0.1",
                "-U",
                "postgres",
                "-d",
                "pyracast",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM django_migrations) "
                "OR to_regclass('forecasts_collectionjob') IS NULL "
                "OR to_regclass('notifications_notificationdelivery') IS NULL "
                "THEN RAISE EXCEPTION 'Restored application schema is incomplete'; END IF; END $$;",
            ],
            check=True,
        )
        report("pyracast_restore_succeeded", "INFO", object=name)
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=False)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True, check=False)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("host_config", type=Path)
    parser.add_argument("--release-dir", type=Path, default=host.CURRENT_RELEASE)
    parser.add_argument("--kind", choices=("hourly", "prerelease"), default="hourly")
    parser.add_argument("--rehearse", metavar="OBJECT")
    options = parser.parse_args(arguments)
    config = json.loads(options.host_config.read_text())
    bucket = config["backup_bucket"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", bucket):
        raise ValueError("Invalid backup bucket.")
    with tempfile.TemporaryDirectory(prefix="pyracast-backup-") as temporary:
        directory = Path(temporary)
        if options.rehearse:
            rehearse_restore(options.release_dir, bucket, options.rehearse, directory)
        elif create_backup(options.release_dir, bucket, options.kind, directory) is None:
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
