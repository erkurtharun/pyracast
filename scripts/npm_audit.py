import argparse
import json
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, TypedDict

GITHUB_ADVISORY = re.compile(r"https://github\.com/advisories/(GHSA(?:-[0-9a-z]{4}){3})")
EXCEPTION_KEYS = {"advisory", "package", "reason", "expires"}
# npm audit exits 1 exactly when it reports vulnerabilities; any other status is a failed audit.
AUDIT_STATUSES = {0, 1}


class AuditException(TypedDict):
    advisory: str
    package: str
    reason: str
    expires: str


def advisory_id(cause: dict[str, Any]) -> str:
    match = GITHUB_ADVISORY.fullmatch(str(cause.get("url", "")).rstrip("/"))
    if match:
        return match.group(1)
    source = cause.get("source")
    if isinstance(source, int) and not isinstance(source, bool):
        return f"npm-{source}"
    raise ValueError(f"Advisory without an identity: {cause!r}")


def leads_to_advisory(
    name: str, direct: dict[str, set[str]], references: dict[str, set[str]]
) -> bool:
    pending, seen = [name], set[str]()
    while pending:
        current = pending.pop()
        if current not in seen:
            seen.add(current)
            if direct[current]:
                return True
            pending.extend(references[current])
    return False


def reported_advisories(report: Any) -> set[str]:
    if not isinstance(report, dict) or "error" in report:
        raise ValueError(f"npm audit failed: {report!r}")
    vulnerabilities = report.get("vulnerabilities")
    if report.get("auditReportVersion") != 2 or not isinstance(vulnerabilities, dict):
        raise ValueError("Unsupported npm audit report.")
    direct: dict[str, set[str]] = {}
    references: dict[str, set[str]] = {}
    for name, package in vulnerabilities.items():
        causes = package.get("via") if isinstance(package, dict) else None
        if not isinstance(causes, list) or not causes:
            raise ValueError(f"Vulnerability {name} has no causes.")
        direct[name], references[name] = set(), set()
        for cause in causes:
            if isinstance(cause, dict):
                direct[name].add(advisory_id(cause))
            elif isinstance(cause, str) and cause in vulnerabilities:
                references[name].add(cause)
            else:
                raise ValueError(f"Vulnerability {name} has an unknown cause: {cause!r}")
    for name in vulnerabilities:
        if not leads_to_advisory(name, direct, references):
            raise ValueError(f"Vulnerability {name} leads to no advisory.")
    return set().union(*direct.values())


def load_exceptions(text: str) -> list[AuditException]:
    entries = json.loads(text)
    if not isinstance(entries, list):
        raise ValueError("Audit exceptions must be a list.")
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != EXCEPTION_KEYS
            or not all(isinstance(value, str) for value in entry.values())
        ):
            raise ValueError(f"Malformed audit exception: {entry!r}")
        date.fromisoformat(entry["expires"])
    exceptions: list[AuditException] = entries
    return exceptions


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


def run_audit(web: Path) -> Any:
    npm = shutil.which("npm")
    if npm is None:
        raise FileNotFoundError("npm is not on PATH.")
    result = subprocess.run(  # noqa: S603 -- fixed npm arguments; no shell.
        [npm, "audit", "--json"], cwd=web, capture_output=True, text=True, check=False
    )
    if result.returncode not in AUDIT_STATUSES:
        raise RuntimeError(f"npm audit exited with {result.returncode}: {result.stderr.strip()}")
    report = json.loads(result.stdout)
    if (
        isinstance(report, dict)
        and "error" not in report
        and result.returncode != int(bool(report.get("vulnerabilities")))
    ):
        raise RuntimeError(
            f"npm audit exited with {result.returncode}, contradicting its report: "
            f"{result.stderr.strip()}"
        )
    return report


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail on every npm advisory except dated, reasoned exceptions."
    )
    parser.add_argument("web", type=Path)
    options = parser.parse_args(arguments)
    exceptions = load_exceptions(
        (options.web / "audit-exceptions.json").read_text(encoding="utf-8")
    )
    failures = audit_failures(reported_advisories(run_audit(options.web)), exceptions, date.today())
    for failure in failures:
        print(failure)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
