import os
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

# What the dependency audits read: the exported Python pins, the interpreter that evaluates their
# markers, the npm manifest and lockfile, the npm exceptions and the audit's own code.
AUDIT_INPUTS = frozenset(
    {
        ".github/workflows/ci.yml",
        ".github/workflows/dependency-audit.yml",
        ".python-version",
        "pyproject.toml",
        "scripts/npm_audit.py",
        "uv.lock",
        "web/audit-exceptions.json",
        "web/package-lock.json",
        "web/package.json",
    }
)
GIT_TIMEOUT_SECONDS = 25


def audit_owed(event: str, changed: Iterable[str]) -> bool:
    return event != "pull_request" or not AUDIT_INPUTS.isdisjoint(changed)


def merged_changes(root: Path) -> list[str]:
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("git")
    # A pull request run checks out GitHub's merge commit, whose first parent is the base.
    result = subprocess.run(  # noqa: S603 -- Resolved git executable; fixed arguments.
        [git, "-C", str(root), "diff", "--name-only", "--no-renames", "-z", "HEAD^1", "HEAD"],
        capture_output=True,
        check=True,
        timeout=GIT_TIMEOUT_SECONDS,
        env={name: value for name, value in os.environ.items() if not name.startswith("GIT_")},
    )
    return [path for path in result.stdout.decode("utf-8").split("\0") if path]


def main(environ: Mapping[str, str] = os.environ) -> int:
    event = environ["GITHUB_EVENT_NAME"]
    changed = merged_changes(Path.cwd()) if event == "pull_request" else []
    print(f"audit={str(audit_owed(event, changed)).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
