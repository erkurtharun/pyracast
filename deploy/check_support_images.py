import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Finding:
    target: str
    vulnerability: str
    package: str
    version: str


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def handler_names(value: object) -> set[str]:
    handlers: set[str] = set()
    if isinstance(value, dict):
        if "handler" in value:
            if not isinstance(value["handler"], str):
                raise ValueError("Invalid gateway handler.")
            handlers.add(value["handler"])
        for child in value.values():
            handlers.update(handler_names(child))
    elif isinstance(value, list):
        for child in value:
            handlers.update(handler_names(child))
    return handlers


def check_support_images(directory: Path, root: Path, today: date) -> dict[str, tuple[int, int]]:
    policy: dict[str, Any] = json.loads((root / "deploy/gateway/review.json").read_text())
    if today >= date.fromisoformat(policy["expires"]):
        raise ValueError("Gateway vulnerability review expired; reassess before deployment.")
    if file_digest(directory / "caddy") != policy["binary_sha256"]:
        raise ValueError("Gateway binary changed; reassess its vulnerability review.")
    configuration = policy["configuration_sha256"]
    if set(configuration) != {"deploy/Caddyfile", "deploy/routes.caddy"} or any(
        file_digest(root / path) != digest for path, digest in configuration.items()
    ):
        raise ValueError("Gateway configuration changed; reassess exposed code paths.")
    adapted: dict[str, Any] = json.loads((directory / "adapted.json").read_text())
    allowed = {"subroute", "headers", "encode", "request_body", "reverse_proxy"}
    if adapted.get("admin", {}).get("disabled") is not True or (
        handler_names(adapted) - allowed or "pki" in adapted.get("apps", {})
    ):
        raise ValueError(
            "Gateway enables an unreviewed handler, PKI endpoint or administration API."
        )
    reviewed = Finding(**policy["finding"])
    counts: dict[str, tuple[int, int]] = {}
    for service in ("gateway", "rabbitmq", "sqlproxy"):
        report: dict[str, Any] = json.loads((directory / f"{service}.json").read_text())
        expected = (directory / f"{service}.image").read_text().strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected) or (
            report["Metadata"]["ImageID"] != expected
        ):
            raise ValueError(f"{service}: scan does not match the selected immutable image.")
        if not report["Metadata"]["OS"]["Family"] or not report["Results"]:
            raise ValueError(f"{service}: image inventory is missing.")
        raw_count = reviewed_count = 0
        for result in report["Results"]:
            for item in result.get("Vulnerabilities", []):
                raw_count += 1
                finding = Finding(
                    result["Target"],
                    item["VulnerabilityID"],
                    item["PkgName"],
                    item["InstalledVersion"],
                )
                if service == "gateway" and finding == reviewed:
                    reviewed_count += 1
                else:
                    raise ValueError(f"{service}: unreviewed vulnerability: {finding}")
        counts[service] = raw_count, reviewed_count
    if counts["gateway"][1] != 1:
        raise ValueError("Gateway finding changed or disappeared; update the vulnerability review.")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    arguments = parser.parse_args()
    try:
        counts = check_support_images(arguments.reports, arguments.root, date.today())
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"Support image gate failed: {error}") from None
    for service, (raw, reviewed) in counts.items():
        print(f"{service}: raw HIGH/CRITICAL={raw}, reviewed not affected={reviewed}")


if __name__ == "__main__":
    main()
