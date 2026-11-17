import json
import re
import sys
from pathlib import Path

RECORD = "prerelease-backup.json"
# The installer prints the name into a shell command: no whitespace, quotes, NUL or dot segments.
OBJECT = re.compile(r"postgres(?:/[A-Za-z0-9_.-]+)+\.dump")


def object_name(record: Path) -> str | None:
    try:
        value = json.loads(record.read_text(encoding="utf-8"))["object"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(value, str) or not OBJECT.fullmatch(value):
        return None
    if any(part in {".", ".."} for part in value.split("/")):
        return None
    return value


def main(arguments: list[str] | None = None) -> int:
    (record,) = sys.argv[1:] if arguments is None else arguments
    name = object_name(Path(record))
    if name is None:
        return 1
    print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
