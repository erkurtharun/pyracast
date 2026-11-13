import argparse
import json
import re
from pathlib import Path
from typing import Any

SERVICES = ("gateway", "rabbitmq", "postgres")


def check_support_images(directory: Path) -> None:
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
        for result in report["Results"]:
            vulnerabilities = result.get("Vulnerabilities") or []
            if vulnerabilities:
                item = vulnerabilities[0]
                raise ValueError(
                    f"{service}: vulnerability {item['VulnerabilityID']} in "
                    f"{item['PkgName']} {item['InstalledVersion']} ({result['Target']})."
                )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", type=Path)
    arguments = parser.parse_args()
    try:
        check_support_images(arguments.reports)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"Support image gate failed: {error}") from None
    for service in SERVICES:
        print(f"{service}: HIGH/CRITICAL=0")


if __name__ == "__main__":
    main()
