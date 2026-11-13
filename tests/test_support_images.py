import json
from pathlib import Path
from typing import Any

import pytest

from deploy.check_support_images import check_support_images


@pytest.fixture
def support_reports(tmp_path: Path) -> Path:
    identity = "sha256:" + "a" * 64
    for service in ("gateway", "rabbitmq", "postgres"):
        (tmp_path / f"{service}.image").write_text(identity)
        report = {
            "Metadata": {"ImageID": identity, "OS": {"Family": "debian"}},
            "Results": [{"Target": service, "Packages": [{"Name": "example"}]}],
        }
        (tmp_path / f"{service}.json").write_text(json.dumps(report))
    return tmp_path


def test_support_images_accept_complete_clean_reports(support_reports: Path) -> None:
    check_support_images(support_reports)


@pytest.mark.parametrize("service", ["gateway", "rabbitmq", "postgres"])
def test_support_images_reject_even_previously_reviewed_vulnerability(
    support_reports: Path, service: str
) -> None:
    path = support_reports / f"{service}.json"
    report = json.loads(path.read_text())
    report["Results"][0]["Vulnerabilities"] = [
        {
            "VulnerabilityID": "CVE-2026-116587",
            "PkgName": "github.com/go-jose/go-jose/v3",
            "InstalledVersion": "v3.0.5",
        }
    ]
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match=f"{service}: vulnerability CVE-2026-116587"):
        check_support_images(support_reports)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("image", "scan does not match"),
        ("identity", "scan does not match"),
        ("os", "image inventory is missing"),
        ("inventory", "image inventory is missing"),
        ("packages", "package inventory is missing"),
    ],
)
def test_support_images_reject_mismatched_or_incomplete_scan(
    support_reports: Path, change: str, message: str
) -> None:
    path = support_reports / "gateway.json"
    report: dict[str, Any] = json.loads(path.read_text())
    if change == "image":
        report["Metadata"]["ImageID"] = "sha256:" + "b" * 64
    elif change == "identity":
        (support_reports / "gateway.image").write_text("gateway:latest")
    elif change == "os":
        report["Metadata"]["OS"]["Family"] = ""
    elif change == "inventory":
        report["Results"] = []
    elif change == "packages":
        report["Results"][0]["Packages"] = []
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match=message):
        check_support_images(support_reports)


@pytest.mark.parametrize("service", ["gateway", "rabbitmq", "postgres"])
def test_support_images_require_every_service_report(support_reports: Path, service: str) -> None:
    (support_reports / f"{service}.json").unlink()
    with pytest.raises(FileNotFoundError):
        check_support_images(support_reports)
