import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    commands = [
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "mypy"],
        [sys.executable, "-m", "pytest"],
    ]
    for command in commands:
        print(f"Running: {' '.join(command)}", flush=True)
        result = subprocess.run(command, cwd=root, check=False)  # noqa: S603 -- fixed tools.
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
