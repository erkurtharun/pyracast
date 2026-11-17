import argparse
import json
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, TypedDict


class AuditException(TypedDict):
    advisory: str
    package: str
    reason: str
    expires: str


def reported_advisories(report: dict[str, Any]) -> set[str]:
    if "error" in report:
        raise ValueError(f"npm audit failed: {report['error']}")
    found: set[str] = set()
    for package in report["vulnerabilities"].values():
        for cause in package["via"]:
            if isinstance(cause, dict):
                found.add(cause["url"].rstrip("/").rsplit("/", 1)[-1])
    return found


def audit_failures(found: set[str], exceptions: list[AuditException], today: date) -> list[str]:
    failures: list[str] = []
    accepted: set[str] = set()
    for entry in exceptions:
        if not entry["reason"].strip():
            failures.append(f"Audit exception {entry['advisory']} has no reason.")
        elif date.fromisoformat(entry["expires"]) < today:
            failures.append(f"Audit exception {entry['advisory']} expired on {entry['expires']}.")
        else:
            accepted.add(entry["advisory"])
    failures += [f"Advisory {advisory} is not accepted." for advisory in sorted(found - accepted)]
    failures += [
        f"Audit exception {advisory} is no longer reported; remove it."
        for advisory in sorted(accepted - found)
    ]
    return failures


def run_audit(web: Path) -> dict[str, Any]:
    npm = shutil.which("npm")
    if npm is None:
        raise FileNotFoundError("npm is not on PATH.")
    result = subprocess.run(  # noqa: S603 -- fixed npm arguments; no shell.
        [npm, "audit", "--json"], cwd=web, capture_output=True, text=True, check=False
    )
    report: dict[str, Any] = json.loads(result.stdout)
    return report


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail on every npm advisory except dated, reasoned exceptions."
    )
    parser.add_argument("web", type=Path)
    options = parser.parse_args(arguments)
    exceptions: list[AuditException] = json.loads(
        (options.web / "audit-exceptions.json").read_text(encoding="utf-8")
    )
    failures = audit_failures(reported_advisories(run_audit(options.web)), exceptions, date.today())
    for failure in failures:
        print(failure)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
