import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from deploy.check_support_images import check_support_images, file_digest


@pytest.fixture
def support_reports(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    root = tmp_path / "source"
    directory = tmp_path / "reports"
    (root / "deploy/gateway").mkdir(parents=True)
    directory.mkdir()
    for name in ("Caddyfile", "routes.caddy"):
        (root / f"deploy/{name}").write_text("reviewed configuration")
    (directory / "caddy").write_bytes(b"reviewed binary")
    finding = {
        "VulnerabilityID": "CVE-2026-116587",
        "PkgName": "github.com/go-jose/go-jose/v3",
        "InstalledVersion": "v3.0.5",
    }
    identity = "sha256:" + "a" * 64
    report = {
        "Metadata": {"ImageID": identity, "OS": {"Family": "alpine"}},
        "Results": [{"Target": "usr/bin/caddy", "Vulnerabilities": [finding]}],
    }
    for service in ("gateway", "rabbitmq", "sqlproxy"):
        (directory / f"{service}.image").write_text(identity)
        value = report if service == "gateway" else dict(report, Results=[{"Target": service}])
        (directory / f"{service}.json").write_text(json.dumps(value))
    (directory / "adapted.json").write_text(
        json.dumps({"admin": {"disabled": True}, "apps": {"http": {"handler": "reverse_proxy"}}})
    )
    policy = {
        "expires": "2026-11-27",
        "binary_sha256": file_digest(directory / "caddy"),
        "configuration_sha256": {
            f"deploy/{name}": file_digest(root / f"deploy/{name}")
            for name in ("Caddyfile", "routes.caddy")
        },
        "finding": {
            "target": "usr/bin/caddy",
            "vulnerability": finding["VulnerabilityID"],
            "package": finding["PkgName"],
            "version": finding["InstalledVersion"],
        },
    }
    (root / "deploy/gateway/review.json").write_text(json.dumps(policy))
    return directory, root, report


def test_review_reports_raw_findings_without_claiming_zero(
    support_reports: tuple[Path, Path, dict[str, Any]],
) -> None:
    directory, root, _ = support_reports
    assert check_support_images(directory, root, date(2026, 11, 13)) == {
        "gateway": (1, 1),
        "rabbitmq": (0, 0),
        "sqlproxy": (0, 0),
    }


@pytest.mark.parametrize(
    "change",
    [
        "image",
        "expiry",
        "binary",
        "configuration",
        "vulnerability",
        "package",
        "version",
        "target",
        "inventory",
        "missing",
        "other_service",
        "admin",
        "handler",
        "pki",
    ],
)
def test_review_fails_closed_when_scope_or_findings_change(
    support_reports: tuple[Path, Path, dict[str, Any]], change: str
) -> None:
    directory, root, original = support_reports
    report = copy.deepcopy(original)
    today = date(2026, 11, 13)
    adapted: dict[str, Any] = {
        "admin": {"disabled": True},
        "apps": {"http": {"handler": "reverse_proxy"}},
    }
    if change == "image":
        report["Metadata"]["ImageID"] = "sha256:" + "b" * 64
    elif change == "expiry":
        today = date(2026, 11, 27)
    elif change == "binary":
        (directory / "caddy").write_bytes(b"different binary")
    elif change == "configuration":
        (root / "deploy/routes.caddy").write_text("acme_server")
    elif change in {"vulnerability", "package", "version"}:
        field = {
            "vulnerability": "VulnerabilityID",
            "package": "PkgName",
            "version": "InstalledVersion",
        }[change]
        report["Results"][0]["Vulnerabilities"][0][field] = "different"
    elif change == "target":
        report["Results"][0]["Target"] = "another/binary"
    elif change == "inventory":
        report["Results"] = []
    elif change == "missing":
        report["Results"][0]["Vulnerabilities"] = []
    elif change == "other_service":
        (directory / "rabbitmq.json").write_text(json.dumps(report))
    elif change == "admin":
        adapted["admin"]["disabled"] = False
    elif change == "handler":
        adapted["apps"]["http"]["handler"] = "acme_server"
    elif change == "pki":
        adapted["apps"]["pki"] = {}
    (directory / "adapted.json").write_text(json.dumps(adapted))
    (directory / "gateway.json").write_text(json.dumps(report))
    with pytest.raises(
        ValueError, match=r"scan does not match|expired|changed|unreviewed|inventory"
    ):
        check_support_images(directory, root, today)
