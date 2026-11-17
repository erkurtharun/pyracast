import argparse
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING or __package__:
    from scripts.pytest_shard import finish_shard, parse_shard, validate_shards
else:
    from pytest_shard import finish_shard, parse_shard, validate_shards

XDIST_WORKERS = "PYTEST_XDIST_AUTO_NUM_WORKERS"
# Each worker runs its own interpreter and test database; the cap bounds peak memory on 16 GB hosts.
MAX_XDIST_WORKERS = 6


def xdist_workers(environment: dict[str, str]) -> str:
    if explicit := environment.get(XDIST_WORKERS):
        # xdist ignores a value int() rejects and falls back to every physical core.
        try:
            int(explicit)
        except ValueError:
            raise ValueError(f"{XDIST_WORKERS} must be an integer, got {explicit!r}") from None
        return explicit
    cores = psutil.cpu_count(logical=False) or os.cpu_count() or 1
    return str(min(cores, MAX_XDIST_WORKERS))


def arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--shard")
    mode.add_argument("--combine-shards", nargs="+", type=Path)
    parser.add_argument("--shard-output", type=Path)
    args = parser.parse_args(argv)
    if args.shard:
        try:
            parse_shard(args.shard)
        except ValueError as error:
            parser.error(str(error))
        if args.shard_output is None:
            parser.error("--shard requires --shard-output")
    elif args.shard_output is not None:
        parser.error("--shard-output requires --shard")
    return args


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    shard_output: Path | None = None
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as stream:
        settings = tomllib.load(stream)
    coverage_floor = settings["tool"]["coverage"]["report"]["fail_under"]
    environment = os.environ.copy()
    if not args.combine_shards:
        try:
            environment[XDIST_WORKERS] = xdist_workers(environment)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 2
        print(f"pytest-xdist workers: {environment[XDIST_WORKERS]}", flush=True)
    commands = [
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        [sys.executable, "-m", "ruff", "check", "--output-format", "concise", "."],
        [sys.executable, "-m", "mypy"],
        [sys.executable, "scripts/translations.py", "--check"],
        [sys.executable, "scripts/generate_er_diagram.py", "--check"],
        [
            sys.executable,
            "-m",
            "django",
            "makemigrations",
            "--settings=pyracast.settings",
            "--check",
            "--dry-run",
        ],
        [sys.executable, "-m", "pytest", "-n", "auto", f"--cov-fail-under={coverage_floor}"],
    ]
    if args.shard:
        output = args.shard_output.resolve()
        shard_output = output
        output.mkdir(parents=True, exist_ok=False)
        environment.update(
            PYRACAST_TEST_SHARD=args.shard,
            PYRACAST_TEST_SHARD_OUTPUT=str(output),
            COVERAGE_FILE=str(output / "coverage.dat"),
        )
        options = [
            option
            for option in settings["tool"]["pytest"]["addopts"]
            if not option.startswith("--cov-report")
        ]
        # Partial measurements are gated only after every shard is combined at the shared floor.
        commands[-1] = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "scripts.pytest_shard",
            "-n",
            "auto",
            "-o",
            f"addopts={shlex.join(options)}",
            "--cov-report=",
            "--cov-fail-under=0",
        ]
    elif args.combine_shards:
        try:
            files = validate_shards([path.resolve() for path in args.combine_shards])
        except (ValueError, OSError) as error:
            print(f"Invalid test shards: {error}", file=sys.stderr)
            return 2
        commands = [
            [sys.executable, "-m", "coverage", "combine", "--keep", *(str(path) for path in files)],
            [sys.executable, "-m", "coverage", "report", f"--fail-under={coverage_floor}"],
            [sys.executable, "-m", "coverage", "xml"],
        ]
    for command in commands:
        print(f"Running: {' '.join(command)}", flush=True)
        result = subprocess.run(command, cwd=root, env=environment, check=False)  # noqa: S603
        if result.returncode:
            return result.returncode
    if shard_output is not None:
        finish_shard(shard_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
