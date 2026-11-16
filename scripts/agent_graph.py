import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path


def graph_directory(root: Path, environment: Mapping[str, str], *, windows: bool) -> Path:
    override = environment.get("CRG_HOME")
    if override:
        directory = Path(override).expanduser()
    else:
        variable = "LOCALAPPDATA" if windows else "XDG_CACHE_HOME"
        default = Path.home() / ("AppData/Local" if windows else ".cache")
        cache = Path(environment.get(variable) or default).expanduser()
        identity = str(root.resolve())
        digest = hashlib.sha256((identity.casefold() if windows else identity).encode()).hexdigest()
        directory = cache / "code-review-graph" / f"{root.name}-{digest[:12]}"
    if not directory.is_absolute():
        raise ValueError("The graph cache must be an absolute path outside the repository.")
    directory = directory.resolve()
    if directory.is_relative_to(root.resolve()):
        raise ValueError("The graph cache must stay outside the repository, including symlinks.")
    return directory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Code Review Graph with an external cache.")
    operations = parser.add_subparsers(dest="operation", required=True)
    operations.add_parser("build")
    operations.add_parser("update")
    query = operations.add_parser("query")
    query.add_argument(
        "pattern",
        choices=(
            "callers_of",
            "callees_of",
            "imports_of",
            "importers_of",
            "children_of",
            "tests_for",
            "inheritors_of",
            "file_summary",
        ),
    )
    query.add_argument("target")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        environment = os.environ.copy()
        directory = graph_directory(root, environment, windows=sys.platform == "win32")
        executable = shutil.which("code-review-graph")
        if executable is None:
            raise FileNotFoundError("Install the user-level code-review-graph CLI before using it.")
        environment["CRG_HOME"] = str(directory)
        # Queries otherwise fall back into the checkout before a registry entry exists.
        environment["CRG_DATA_DIR"] = str(directory)
        command = [executable, args.operation]
        if args.operation == "query":
            command.extend([args.pattern, args.target])
        else:
            command.extend(["--skip-flows", "--data-dir", str(directory)])
        command.extend(["--repo", str(root)])
        return subprocess.run(  # noqa: S603 -- fixed executable and parsed argument list; no shell.
            command, cwd=root, env=environment, check=False
        ).returncode
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
