import json
import re
import sys
from pathlib import Path

RECORD = "prerelease-backup.json"
# deploy/reset_database.py keeps its maintenance session here, valid while the marker holds its
# identity; once reset replaced the database, only the session's archive holds its data.
SESSION = Path("/var/lib/pyracast/reset/session.json")
MARKER = Path("/var/lib/pyracast/release-blocked")
# The installer prints the name into a shell command: no whitespace, quotes, NUL or dot segments.
OBJECT = re.compile(r"postgres(?:/[A-Za-z0-9_.-]+)+\.dump")


def valid_object(value: object) -> str | None:
    if not isinstance(value, str) or not OBJECT.fullmatch(value):
        return None
    if any(part in {".", ".."} for part in value.split("/")):
        return None
    return value


def object_name(record: Path) -> str | None:
    try:
        value = json.loads(record.read_text(encoding="utf-8"))["object"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return valid_object(value)


def replaced_object(session: Path, marker: Path) -> str | None:
    try:
        data = json.loads(session.read_text(encoding="utf-8"))
        identity = marker.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("replaced") is not True:
        return None
    if data.get("marker") != identity:
        return None
    return valid_object(data.get("object"))


def main(arguments: list[str] | None = None) -> int:
    match sys.argv[1:] if arguments is None else arguments:
        case ["--replaced-by-reset"]:
            name = replaced_object(SESSION, MARKER)
        case [record]:
            name = object_name(Path(record))
        case _:
            return 2
    if name is None:
        return 1
    print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
