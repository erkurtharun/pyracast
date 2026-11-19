# ruff: noqa: S603, S607 -- Fixed systemd and bash executables with argument lists; the stage is validated.
import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

STAGE = re.compile(r"/run/pyracast-release-[0-9a-f]{40}-[0-9]+-[0-9]+")
STATUS = "status"
CURSOR = "journal.cursor"
POLL_SECONDS = 5
# ssh exits 255 when the connection fails; the workflow follows the unit again on that status.
CONNECTION_LOST = 255


def stage_directory(value: str) -> Path:
    if STAGE.fullmatch(value) is None:
        raise ValueError(f"Not a release stage: {value!r}")
    return Path(value)


def unit_name(stage: Path) -> str:
    return f"{stage.name}.service"


def start(stage: Path, arguments: Sequence[str]) -> int:
    subprocess.run(
        [
            "systemd-run",
            f"--unit={unit_name(stage)}",
            "--service-type=exec",
            f"--description=Pyracast release {arguments[0]}",
            "--quiet",
            "--",
            sys.executable,
            str(stage / "bundle" / "deploy" / "release_unit.py"),
            "run",
            str(stage),
            *arguments,
        ],
        check=True,
    )
    return follow(stage)


def run(stage: Path, arguments: Sequence[str]) -> int:
    bundle = stage / "bundle"
    result = subprocess.run(
        [
            "bash",
            str(bundle / "deploy" / "install-release.sh"),
            *arguments,
            str(bundle),
            str(stage / "migration.json"),
        ],
        check=False,
    )
    status = 128 - result.returncode if result.returncode < 0 else result.returncode
    descriptor, temporary = tempfile.mkstemp(dir=stage)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(f"{status}\n")
    os.replace(temporary, stage / STATUS)
    return status


def _status(stage: Path) -> int | None:
    try:
        content = (stage / STATUS).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    if re.fullmatch(r"[0-9]{1,3}\n", content) is None or int(content) > 255:
        raise ValueError(f"Unreadable installer status in {stage / STATUS}")
    return int(content)


def follow(stage: Path) -> int:
    unit = unit_name(stage)
    while True:
        # The unit writes its status before it ends, so an ended unit without one never wrote it.
        active = subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False)
        status = _status(stage)
        sys.stdout.flush()
        subprocess.run(
            [
                "journalctl",
                f"--unit={unit}",
                f"--cursor-file={stage / CURSOR}",
                "--output=cat",
                "--no-pager",
                "--quiet",
            ],
            check=False,
        )
        if status is not None:
            return 1 if status == CONNECTION_LOST else status
        if active.returncode != 0:
            print(
                f"The release unit {unit} is not running and recorded no status: it did not "
                "start, or it was killed or the host restarted. See 'A release that fails "
                "during maintenance' in the deployment guide.",
                file=sys.stderr,
            )
            return 1
        time.sleep(POLL_SECONDS)


def _stage(value: str) -> Path:
    try:
        return stage_directory(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "run"):
        command = commands.add_parser(name)
        command.add_argument("stage", type=_stage)
        command.add_argument("installer_arguments", nargs=6, metavar="SHA_AND_IMAGES")
    commands.add_parser("follow").add_argument("stage", type=_stage)
    options = parser.parse_args(arguments)
    if options.command == "start":
        return start(options.stage, options.installer_arguments)
    if options.command == "run":
        return run(options.stage, options.installer_arguments)
    return follow(options.stage)


if __name__ == "__main__":
    raise SystemExit(main())
