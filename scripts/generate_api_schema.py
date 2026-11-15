import argparse
import os
import subprocess
import sys
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).resolve().parents[1] / "web/openapi.json"
    )
    output = parser.parse_args(argv).output.resolve()
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("PYRACAST_", "DJANGO_", "PG"))
    }
    environment["PYRACAST_SECRET_KEY"] = token_urlsafe(48)
    # A clean working directory prevents the application from loading a local .env file.
    with TemporaryDirectory(prefix="pyracast-schema-") as directory:
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "django",
                "spectacular",
                "--settings=pyracast.settings",
                "--validate",
                "--fail-on-warn",
                "--format=openapi-json",
                f"--file={output}",
            ],
            cwd=directory,
            env=environment,
            check=False,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
