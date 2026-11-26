import argparse
import json
import math
import os
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING
from xml.parsers import expat

if TYPE_CHECKING or __package__:
    from scripts.pytest_shard import DURATIONS, read_durations, usable_seconds
else:
    from pytest_shard import DURATIONS, read_durations, usable_seconds

SYSTEMS = ("Linux", "Windows")
TEST_MODULE_PATTERNS = ("test_*.py", "*_test.py")
JUnitCase = tuple[str, str, float]


def junit_cases(path: Path, root: Path) -> list[JUnitCase]:
    location = os.path.realpath(path)
    if not location.startswith(os.path.realpath(root) + os.sep):
        raise ValueError(f"{path}: a result must lie inside the checkout")
    cases: list[JUnitCase] = []

    def start(name: str, attributes: dict[str, str]) -> None:
        if name != "testcase":
            return
        classname, test, seconds = (attributes.get(key) for key in ("classname", "name", "time"))
        if classname is None or test is None or seconds is None:
            raise ValueError(f"{path}: a testcase lacks its class name, name or time")
        duration = float(seconds)
        if not usable_seconds(duration):
            raise ValueError(f"{path}: invalid time {seconds!r}")
        cases.append((classname, test, duration))

    def refuse_document_type(*_: object) -> None:
        raise ValueError(f"{path}: a document type declaration is not accepted")

    parser = expat.ParserCreate()
    parser.StartElementHandler = start
    parser.StartDoctypeDeclHandler = refuse_document_type
    with open(location, "rb") as stream:
        parser.ParseFile(stream)
    return cases


def module_inventory(root: Path) -> frozenset[str]:
    return frozenset(
        path.relative_to(root).as_posix()
        for pattern in TEST_MODULE_PATTERNS
        for path in (root / "tests").rglob(pattern, case_sensitive=True)
        if path.is_file()
    )


def module_file(classname: str, modules: frozenset[str]) -> str | None:
    parts = classname.split(".")
    for end in range(1, len(parts) + 1):
        candidate = "/".join(parts[:end]) + ".py"
        if candidate in modules:
            return candidate
    return None


def module_durations(results: Iterable[Path], root: Path) -> dict[str, float]:
    modules = module_inventory(root)
    seen: set[tuple[str, str]] = set()
    times: dict[str, list[float]] = defaultdict(list)
    for path in results:
        for classname, test, seconds in junit_cases(path, root):
            if (classname, test) in seen:
                raise ValueError(f"{classname}::{test} appears in more than one result")
            seen.add((classname, test))
            if module := module_file(classname, modules):
                times[module].append(seconds)
    durations = {module: round(math.fsum(values), 1) for module, values in sorted(times.items())}
    for module, seconds in durations.items():
        if not usable_seconds(seconds):
            raise ValueError(f"{module}: {seconds} seconds is more than one day")
    return durations


def replace_file(path: Path, text: str) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record each test module's duration from the JUnit results of one CI run."
    )
    parser.add_argument("system", choices=SYSTEMS)
    parser.add_argument("results", nargs="+", type=Path, help="pytest JUnit XML files")
    args = parser.parse_args(argv)
    try:
        recorded = read_durations(DURATIONS)
        figures = module_durations(args.results, DURATIONS.parents[1])
        if not figures:
            raise ValueError("no result names a test module of this checkout")
        recorded[args.system] = figures
        replace_file(DURATIONS, json.dumps(recorded, indent=2, sort_keys=True) + "\n")
    except (ValueError, OSError, expat.ExpatError) as error:
        print(f"Cannot record test durations: {error}", file=sys.stderr)
        return 2
    print(f"Recorded {len(figures)} {args.system} test modules in {DURATIONS.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
