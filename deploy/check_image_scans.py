import argparse
import json
import re
from pathlib import Path
from typing import Any

SERVICES = ("backend", "web", "gateway", "rabbitmq", "postgres")


def _findings(service: str, report: dict[str, Any]) -> list[str]:
    return [
        f"{service}: {item['VulnerabilityID']} in {item['PkgName']} {item['InstalledVersion']} "
        f"({result['Target']})"
        for result in report["Results"]
        for item in result.get("Vulnerabilities") or []
    ]


def check_image_scans(directory: Path) -> list[str]:
    findings: list[str] = []
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", type=Path)
    arguments = parser.parse_args()
    try:
        findings = check_image_scans(arguments.reports)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"Image scan gate failed: {error}") from None
    if findings:
        print("\n".join(findings))
        raise SystemExit(f"Image scan gate failed: {len(findings)} high or critical findings.")
    for service in SERVICES:
        print(f"{service}: HIGH/CRITICAL=0")


if __name__ == "__main__":
    main()
