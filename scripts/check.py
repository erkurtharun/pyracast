import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


Stage = tuple[str, list[str]]


def arguments(argv: list[str] | None) -> tuple[argparse.Namespace, list[str]]:
    argv = sys.argv[1:] if argv is None else argv
    split = argv.index("--") if "--" in argv else len(argv)
    pytest_arguments = argv[split + 1 :]
    parser = argparse.ArgumentParser(
        usage="%(prog)s [options] [-- PYTEST_ARGUMENTS...]",
        epilog="Arguments after -- are passed to pytest.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--shard")
    mode.add_argument("--combine-shards", nargs="+", type=Path)
    parser.add_argument("--shard-output", type=Path)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--static-only", action="store_true", help="skip pytest")
    scope.add_argument("--tests-only", action="store_true", help="run only pytest")
    args = parser.parse_args(argv[:split])
    if args.combine_shards and (args.static_only or args.tests_only or pytest_arguments):
        parser.error("--combine-shards takes no stage selection or pytest arguments")
    if args.static_only and (args.shard or pytest_arguments):
        parser.error("--static-only takes no --shard or pytest arguments")
    if args.shard:
        try:
            parse_shard(args.shard)
        except ValueError as error:
            parser.error(str(error))
        if args.shard_output is None:
            parser.error("--shard requires --shard-output")
    elif args.shard_output is not None:
        parser.error("--shard-output requires --shard")
    return args, pytest_arguments


def web_npm(root: Path) -> str | None:
    if not (root / "web" / "node_modules").is_dir():
        return None
    if npm := shutil.which("npm"):
        return npm
    raise FileNotFoundError(
        "web/node_modules exists but npm is missing from PATH; install Node.js or remove "
        "web/node_modules."
    )


def static_stages(root: Path) -> list[Stage]:
    stages: list[Stage] = [
        ("ruff format", [sys.executable, "-m", "ruff", "format", "--check", "."]),
        ("ruff check", [sys.executable, "-m", "ruff", "check", "--output-format", "concise", "."]),
        ("mypy", [sys.executable, "-m", "mypy"]),
        ("translations", [sys.executable, "scripts/translations.py", "--check"]),
        ("ER diagram", [sys.executable, "scripts/generate_er_diagram.py", "--check"]),
        (
            "migrations",
            [
                sys.executable,
                "-m",
                "django",
                "makemigrations",
                "--settings=pyracast.settings",
                "--check",
                "--dry-run",
            ],
        ),
        ("django check", [sys.executable, "-m", "django", "check", "--settings=pyracast.settings"]),
    ]
    npm = web_npm(root)
    if npm is None:
        print(
            "SKIPPED web: web/node_modules is missing; run `npm ci` in web/ to include "
            "the API schema and web static checks.",
            flush=True,
        )
        return stages
    return [
        *stages,
        ("API schema", [sys.executable, "scripts/generate_api_schema.py", "--check"]),
        ("web static checks", [npm, "--prefix", "web", "run", "check:static"]),
    ]


def pytest_stage(
    args: argparse.Namespace,
    settings: dict[str, Any],
    coverage_floor: int,
    pytest_arguments: list[str],
) -> Stage:
    if not args.shard:
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "auto",
            f"--cov-fail-under={coverage_floor}",
        ]
        return "pytest", [*command, *pytest_arguments]
    options = [
        option
        for option in settings["tool"]["pytest"]["addopts"]
        if not option.startswith("--cov-report")
    ]
    # Partial measurements are gated only after every shard is combined at the shared floor.
    command = [
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
    return "pytest", [*command, *pytest_arguments]


def run_stages(stages: list[Stage], root: Path, environment: dict[str, str]) -> int:
    for name, command in stages:
        print(f"Running {name}: {' '.join(command)}", flush=True)
        started = time.perf_counter()
        result = subprocess.run(command, cwd=root, env=environment, check=False)  # noqa: S603
        elapsed = time.perf_counter() - started
        if result.returncode:
            print(f"Failed {name} in {elapsed:.1f}s (exit {result.returncode})", flush=True)
            return result.returncode
        print(f"Finished {name} in {elapsed:.1f}s", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args, pytest_arguments = arguments(argv)
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as stream:
        settings = tomllib.load(stream)
    coverage_floor = settings["tool"]["coverage"]["report"]["fail_under"]
    environment = os.environ.copy()
    if args.combine_shards:
        try:
            files = validate_shards([path.resolve() for path in args.combine_shards])
        except (ValueError, OSError) as error:
            print(f"Invalid test shards: {error}", file=sys.stderr)
            return 2
        combine = [sys.executable, "-m", "coverage", "combine", "--keep", *map(str, files)]
        return run_stages(
            [
                ("coverage combine", combine),
                (
                    "coverage report",
                    [sys.executable, "-m", "coverage", "report", f"--fail-under={coverage_floor}"],
                ),
                ("coverage xml", [sys.executable, "-m", "coverage", "xml"]),
            ],
            root,
            environment,
        )
    if not args.static_only:
        try:
            environment[XDIST_WORKERS] = xdist_workers(environment)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 2
        print(f"pytest-xdist workers: {environment[XDIST_WORKERS]}", flush=True)
    try:
        stages = [] if args.tests_only else static_stages(root)
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 2
    if not args.static_only:
        stages.append(pytest_stage(args, settings, coverage_floor, pytest_arguments))
    shard_output: Path | None = None
    if args.shard:
        shard_output = args.shard_output.resolve()
        shard_output.mkdir(parents=True, exist_ok=False)
        environment.update(
            PYRACAST_TEST_SHARD=args.shard,
            PYRACAST_TEST_SHARD_OUTPUT=str(shard_output),
            COVERAGE_FILE=str(shard_output / "coverage.dat"),
        )
    if code := run_stages(stages, root, environment):
        return code
    if shard_output is not None:
        finish_shard(shard_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
