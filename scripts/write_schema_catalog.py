import os

import django
from django.db import connection

from pyracast.schema_catalog import SNAPSHOT, read_catalog, serialized


def write() -> None:
    """Record the catalog of the configured database, which `migrate` built."""
    recorded = serialized(read_catalog(connection)).encode("utf-8")
    partial = SNAPSHOT.with_name(f"{SNAPSHOT.name}.partial")
    try:
        partial.write_bytes(recorded)
        partial.replace(SNAPSHOT)
    finally:
        partial.unlink(missing_ok=True)


def main() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pyracast.settings")
    django.setup()
    write()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
