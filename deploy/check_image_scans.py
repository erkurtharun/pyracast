import argparse
import json
import re
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, NamedTuple, TypedDict

SERVICES = ("backend", "web", "gateway", "rabbitmq", "postgres")
# scan-images.sh mounts deploy/ read-only beside this script and gives the container no network.
EXCEPTIONS = Path(__file__).with_name("image-scan-exceptions.json")
EXCEPTION_KEYS = {"service", "vulnerability", "package", "reason", "expires"}

type Key = tuple[str, str, str]


class ScanException(TypedDict):
    service: str
    vulnerability: str
    package: str
    reason: str
    expires: str


class Finding(NamedTuple):
    service: str
    vulnerability: str
    package: str
    version: str
    target: str

    @property
    def key(self) -> Key:
        return (self.service, self.vulnerability, self.package)

    def __str__(self) -> str:
        return (
            f"{self.service}: {self.vulnerability} in {self.package} {self.version} ({self.target})"
        )


class Review(NamedTuple):
    accepted: list[tuple[Finding, str]]
    rejected: list[Finding]
    problems: list[str]


def _text(service: str, item: dict[str, Any], field: str) -> str:
    value = item[field]
    if not isinstance(value, str):
        raise ValueError(f"{service}: {field} of a finding is not text.")
    return value


def _findings(service: str, report: dict[str, Any]) -> list[Finding]:
    return [
        Finding(
            service,
            _text(service, item, "VulnerabilityID"),
            _text(service, item, "PkgName"),
            item["InstalledVersion"],
            result["Target"],
        )
        for result in report["Results"]
        for item in result.get("Vulnerabilities") or []
    ]


def check_image_scans(directory: Path) -> list[Finding]:
    findings: list[Finding] = []
    for service in SERVICES:
        report: dict[str, Any] = json.loads((directory / f"{service}.json").read_text())
        expected = (directory / f"{service}.image").read_text().strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected) or (
            report["Metadata"]["ImageID"] != expected
        ):
            raise ValueError(f"{service}: scan does not match the selected immutable image.")
        if not report["Metadata"]["OS"]["Family"] or not report["Results"]:
            raise ValueError(f"{service}: image inventory is missing.")
        if not any(result.get("Packages") for result in report["Results"]):
            raise ValueError(f"{service}: package inventory is missing.")
        findings.extend(_findings(service, report))
    return findings


def load_exceptions(text: str) -> list[ScanException]:
    entries = json.loads(text)
    if not isinstance(entries, list):
        raise ValueError("Image scan exceptions must be a list.")
    seen: set[Key] = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != EXCEPTION_KEYS
            or not all(isinstance(value, str) for value in entry.values())
            or entry["service"] not in SERVICES
            or not entry["vulnerability"].strip()
            or not entry["package"].strip()
            or date.fromisoformat(entry["expires"]).isoformat() != entry["expires"]
        ):
            raise ValueError(f"Malformed image scan exception: {entry!r}")
        key = (entry["service"], entry["vulnerability"], entry["package"])
        if key in seen:
            raise ValueError(f"Duplicate image scan exception: {_label(key)}")
        seen.add(key)
    exceptions: list[ScanException] = entries
    return exceptions


def _label(key: Key) -> str:
    service, vulnerability, package = key
    return f"{service}: {vulnerability} in {package}"


def review_findings(
    findings: list[Finding], exceptions: list[ScanException], today: date
) -> Review:
    problems: list[str] = []
    accepted_until: dict[Key, str] = {}
    for entry in exceptions:
        key = (entry["service"], entry["vulnerability"], entry["package"])
        if not entry["reason"].strip():
            problems.append(f"Image scan exception {_label(key)} has no reason.")
        elif date.fromisoformat(entry["expires"]) < today:
            problems.append(f"Image scan exception {_label(key)} expired on {entry['expires']}.")
        else:
            accepted_until[key] = entry["expires"]
    reported = {finding.key for finding in findings}
    problems += [
        f"Image scan exception {_label(key)} is no longer reported; remove it."
        for key in accepted_until
        if key not in reported
    ]
    return Review(
        [
            (finding, accepted_until[finding.key])
            for finding in findings
            if finding.key in accepted_until
        ],
        [finding for finding in findings if finding.key not in accepted_until],
        problems,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", type=Path)
    arguments = parser.parse_args()
    try:
        findings = check_image_scans(arguments.reports)
        exceptions = load_exceptions(EXCEPTIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"Image scan gate failed: {error}") from None
    review = review_findings(findings, exceptions, datetime.now(UTC).date())
    for finding, expires in review.accepted:
        print(f"{finding} accepted until {expires}")
    for line in [*map(str, review.rejected), *review.problems]:
        print(line)
    if review.rejected or review.problems:
        raise SystemExit(
            f"Image scan gate failed: {len(review.rejected)} high or critical findings and "
            f"{len(review.problems)} exception problems."
        )
    accepted = Counter(finding.service for finding, _ in review.accepted)
    for service in SERVICES:
        print(f"{service}: HIGH/CRITICAL={accepted[service]}, accepted={accepted[service]}")


if __name__ == "__main__":
    main()
