# ruff: noqa: S603, S607 -- Fixed Docker executables with argument lists; no shell interpolation.
import argparse
import base64
import datetime
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy.operations_log import access_token, report


def compose_command(release: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(release / "deploy/release.env"),
        "-f",
        str(release / "deploy/compose.yaml"),
    ]


def upload_backup(bucket: str, name: str, archive: Path) -> None:
    query = urlencode({"uploadType": "media", "name": name, "ifGenerationMatch": "0"})
    url = f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?{query}"
    with archive.open("rb") as stream:
        expected = base64.b64encode(hashlib.file_digest(stream, "md5").digest()).decode()
    for attempt in range(3):
        try:
            with archive.open("rb") as stream:
                request = urllib.request.Request(
                    url,
                    data=stream,
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {access_token()}",
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(archive.stat().st_size),
                    },
                )
                with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310
                    result = json.load(response)
            if (
                not isinstance(result, dict)
                or result.get("md5Hash") != expected
                or result.get("size") != str(archive.stat().st_size)
            ):
                raise ValueError("Uploaded backup checksum does not match.")
            return
        except HTTPError as error:
            error.close()
            if error.code == 412:
                request = urllib.request.Request(
                    f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/"
                    f"{quote(name, safe='')}",
                    headers={"Authorization": f"Bearer {access_token()}"},
                )
                with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                    result = json.load(response)
                if (
                    isinstance(result, dict)
                    and result.get("md5Hash") == expected
                    and result.get("size") == str(archive.stat().st_size)
                ):
                    return
                raise ValueError("Existing backup checksum does not match.") from None
            if attempt == 2:
                raise
            time.sleep(2**attempt)
        except (OSError, ValueError, HTTPException):
            if attempt == 2:
                raise
            time.sleep(2**attempt)


def create_backup(release: Path, bucket: str, kind: str, directory: Path) -> str:
    archive = directory / "database.dump"
    with archive.open("wb") as stream:
        subprocess.run(
            [
                *compose_command(release),
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
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y/%m/%d/%Y%m%dT%H%M%SZ")
    name = f"postgres/{stamp}-{kind}-{uuid.uuid4().hex}.dump"
    upload_backup(bucket, name, archive)
    marker = Path("/var/lib/pyracast/last-backup.json")
    temporary = marker.with_suffix(".next")
    temporary.write_text(json.dumps({"object": name, "completed_at": time.time()}))
    temporary.replace(marker)
    report("pyracast_backup_succeeded", "INFO", object=name, size=archive.stat().st_size)
    return name


def rehearse_restore(release: Path, bucket: str, name: str, directory: Path) -> None:
    if not re.fullmatch(r"postgres/[A-Za-z0-9/_.-]+\.dump", name):
        raise ValueError("Select a PostgreSQL archive object from the configured backup bucket.")
    request = urllib.request.Request(
        f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(name, safe='')}?alt=media",
        headers={"Authorization": f"Bearer {access_token()}"},
    )
    archive = directory / "restore.dump"
    with urllib.request.urlopen(request, timeout=300) as response, archive.open("wb") as stream:  # noqa: S310
        while block := response.read(1024 * 1024):
            stream.write(block)
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("host_config", type=Path)
    parser.add_argument("--release-dir", type=Path, default=Path("/opt/pyracast/current"))
    parser.add_argument("--kind", choices=("hourly", "prerelease"), default="hourly")
    parser.add_argument("--rehearse", metavar="OBJECT")
    arguments = parser.parse_args()
    config = json.loads(arguments.host_config.read_text())
    bucket = config["backup_bucket"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", bucket):
        raise ValueError("Invalid backup bucket.")
    with tempfile.TemporaryDirectory(prefix="pyracast-backup-") as temporary:
        directory = Path(temporary)
        if arguments.rehearse:
            rehearse_restore(arguments.release_dir, bucket, arguments.rehearse, directory)
        else:
            create_backup(arguments.release_dir, bucket, arguments.kind, directory)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        report("pyracast_backup_failed", "ERROR")
        raise SystemExit(1) from None
