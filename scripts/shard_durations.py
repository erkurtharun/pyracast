import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING
from xml.parsers import expat

if TYPE_CHECKING or __package__:
    from scripts.pytest_shard import DURATIONS
else:
    from pytest_shard import DURATIONS

SYSTEMS = ("Linux", "Windows")
JUnitCase = tuple[str, str, float]


def junit_cases(path: Path) -> list[JUnitCase]:
    cases: list[JUnitCase] = []

    def start(name: str, attributes: dict[str, str]) -> None:
        if name != "testcase":
            return
        classname, test, seconds = (attributes.get(key) for key in ("classname", "name", "time"))
        if classname is None or test is None or seconds is None:
            raise ValueError(f"{path}: a testcase lacks its class name, name or time")
        duration = float(seconds)
        if not 0 <= duration < math.inf:
            raise ValueError(f"{path}: invalid time {seconds!r}")
        cases.append((classname, test, duration))

    def refuse_document_type(*_: object) -> None:
        raise ValueError(f"{path}: a document type declaration is not accepted")

    parser = expat.ParserCreate()
    parser.StartElementHandler = start
    parser.StartDoctypeDeclHandler = refuse_document_type
    with path.open("rb") as stream:
        parser.ParseFile(stream)
    return cases


def module_file(classname: str, root: Path) -> str | None:
    parts = classname.split(".")
    if not all(part.isidentifier() for part in parts):
        return None
    for end in range(1, len(parts) + 1):
        candidate = "/".join(parts[:end]) + ".py"
        if (root / candidate).is_file():
            return candidate
    return None


def module_durations(results: Iterable[Path], root: Path) -> dict[str, float]:
    seen: set[tuple[str, str]] = set()
    times: dict[str, list[float]] = defaultdict(list)
    for path in results:
        for classname, test, seconds in junit_cases(path):
            if (classname, test) in seen:
                raise ValueError(f"{classname}::{test} appears in more than one result")
            seen.add((classname, test))
            if module := module_file(classname, root):
                times[module].append(seconds)
    return {module: round(math.fsum(values), 1) for module, values in sorted(times.items())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record each test module's duration from the JUnit results of one CI run."
    )
    parser.add_argument("system", choices=SYSTEMS)
    parser.add_argument("results", nargs="+", type=Path, help="pytest JUnit XML files")
    args = parser.parse_args(argv)
    try:
        recorded: object = json.loads(DURATIONS.read_text(encoding="utf-8"))
        if not isinstance(recorded, dict):
            raise ValueError(f"{DURATIONS} does not hold durations by system")
        figures = module_durations(args.results, DURATIONS.parents[1])
        if not figures:
            raise ValueError("no result names a test module of this checkout")
    except (ValueError, OSError, expat.ExpatError) as error:
        print(f"Cannot record test durations: {error}", file=sys.stderr)
        return 2
    recorded[args.system] = figures
    text = json.dumps(recorded, indent=2, sort_keys=True) + "\n"
    DURATIONS.write_text(text, encoding="utf-8", newline="\n")
    print(f"Recorded {len(figures)} {args.system} test modules in {DURATIONS.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
