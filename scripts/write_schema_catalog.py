import os

import django
from django.db import connection

from pyracast.schema_catalog import SNAPSHOT, read_catalog, serialized


def write() -> None:
    """Record the catalog of the configured database, which `migrate` built."""
    SNAPSHOT.write_bytes(serialized(read_catalog(connection)).encode("utf-8"))


def main() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pyracast.settings")
    django.setup()
    write()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
