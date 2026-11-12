import subprocess
import sys
import tomllib
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as stream:
        settings = tomllib.load(stream)
    coverage_floor = settings["tool"]["coverage"]["report"]["fail_under"]
    commands = [
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        [sys.executable, "-m", "ruff", "check", "--output-format", "concise", "."],
        [sys.executable, "-m", "mypy"],
        [
            sys.executable,
            "-m",
            "django",
            "makemigrations",
            "--settings=pyracast.settings",
            "--check",
            "--dry-run",
        ],
        [sys.executable, "-m", "pytest", "-n", "auto", "-x", f"--cov-fail-under={coverage_floor}"],
    ]
    for command in commands:
        print(f"Running: {' '.join(command)}", flush=True)
        result = subprocess.run(command, cwd=root, check=False)  # noqa: S603 -- fixed tools.
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
