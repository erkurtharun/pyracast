# ruff: noqa: S603, S607 -- Fixed docker executable with an argument list.
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

STATE = Path("/var/lib/pyracast/release-state.json")
RELEASES = Path("/opt/pyracast/releases")
SECRETS = Path("/run/pyracast/releases")
CURRENT = Path("/opt/pyracast/current")
IMAGE_KEYS = ("BACKEND_IMAGE", "WEB_IMAGE", "GATEWAY_IMAGE", "POSTGRES_IMAGE")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def record(state: Path, current: Path, previous: Path | None) -> None:
    payload = {"current": current.name, "previous": None if previous is None else previous.name}
    temporary = state.with_name(f"{state.name}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(state)


def kept_releases(state: Path) -> set[str] | None:
    try:
        payload = json.loads(state.read_text(encoding="utf-8"))
        names = {payload["current"], payload["previous"]} - {None}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if payload["current"] is None or not all(
        isinstance(name, str) and NAME.fullmatch(name) for name in names
    ):
        return None
    return names


def release_images(release: Path) -> set[str]:
    try:
        lines = (release / "deploy/release.env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    values = dict(line.split("=", 1) for line in lines if "=" in line)
    return {values[key] for key in IMAGE_KEYS if values.get(key)}


def prune(
    state: Path,
    releases: Path,
    secrets: Path,
    current: Path,
    remove_image: Callable[[str], None],
    resolve: Callable[[Path], Path] = Path.resolve,
) -> list[str]:
    kept = kept_releases(state)
    if kept is None:
        return [f"{state} is missing or invalid; nothing was pruned."]
    kept.add(resolve(current).name)
    retired = [path for path in releases.iterdir() if path.is_dir() and path.name not in kept]
    used = set[str]().union(*(release_images(releases / name) for name in kept))
    unused = set[str]().union(*(release_images(path) for path in retired)) - used
    problems: list[str] = []
    for reference in sorted(unused):
        try:
            remove_image(reference)
        except (OSError, subprocess.SubprocessError):
            problems.append(f"Image {reference} was not removed.")
    for path in retired:
        shutil.rmtree(path)
        shutil.rmtree(secrets / path.name, ignore_errors=True)
    return problems


def remove_image(reference: str) -> None:
    subprocess.run(["docker", "image", "rm", reference], check=True, capture_output=True)


def main(arguments: list[str]) -> int:
    if arguments[0] == "record":
        previous = Path(arguments[2]) if len(arguments) > 2 else None
        record(STATE, Path(arguments[1]), previous)
        return 0
    problems = prune(STATE, RELEASES, SECRETS, CURRENT, remove_image)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
