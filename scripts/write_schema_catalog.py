import argparse
import os
from pathlib import Path

import django
from django.db import connection

from pyracast.schema_catalog import SNAPSHOT, read_catalog, serialized


def write(output: Path) -> None:
    """Build a throwaway database the way pytest builds its own, record its catalog, drop it."""
    original = connection.settings_dict["NAME"]
    connection.settings_dict["TEST"]["NAME"] = f"{original}_schema_catalog"
    connection.creation.create_test_db(verbosity=0, autoclobber=True, serialize=False)
    try:
        output.write_text(serialized(read_catalog(connection)), encoding="utf-8", newline="\n")
    finally:
        connection.creation.destroy_test_db(original, verbosity=0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=SNAPSHOT)
    args = parser.parse_args(argv)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pyracast.settings")
    django.setup()
    write(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
