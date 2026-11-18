import json
from pathlib import Path
from typing import Any

import pytest

from deploy import check_image_scans
from deploy.check_image_scans import SERVICES
from deploy.check_image_scans import check_image_scans as check

FINDING = {
    "VulnerabilityID": "CVE-2026-116587",
    "PkgName": "github.com/go-jose/go-jose/v3",
    "InstalledVersion": "v3.0.5",
}


@pytest.fixture
def reports(tmp_path: Path) -> Path:
    identity = "sha256:" + "a" * 64
    for service in SERVICES:
        (tmp_path / f"{service}.image").write_text(identity)
        report = {
            "Metadata": {"ImageID": identity, "OS": {"Family": "debian"}},
            "Results": [
                {"Target": service, "Packages": [{"Name": "example"}], "Vulnerabilities": None}
            ],
        }
        (tmp_path / f"{service}.json").write_text(json.dumps(report))
    return tmp_path


def add_finding(reports: Path, service: str, result: int = 0) -> None:
    path = reports / f"{service}.json"
    report = json.loads(path.read_text())
    while len(report["Results"]) <= result:
        report["Results"].append({"Target": f"{service}-{result}", "Packages": []})
    report["Results"][result].setdefault("Vulnerabilities", None)
    findings = report["Results"][result]["Vulnerabilities"] or []
    report["Results"][result]["Vulnerabilities"] = [*findings, FINDING]
    path.write_text(json.dumps(report))


def test_every_deployed_image_is_scanned() -> None:
    assert SERVICES == ("backend", "web", "gateway", "rabbitmq", "postgres")


def test_complete_clean_reports_have_no_findings(reports: Path) -> None:
    assert check(reports) == []


@pytest.mark.parametrize("service", SERVICES)
def test_any_high_or_critical_finding_is_reported(reports: Path, service: str) -> None:
    add_finding(reports, service)

    assert check(reports) == [
        f"{service}: CVE-2026-116587 in github.com/go-jose/go-jose/v3 v3.0.5 ({service})"
    ]


def test_every_finding_of_every_result_is_reported(reports: Path) -> None:
    add_finding(reports, "backend")
    add_finding(reports, "backend")
    add_finding(reports, "web", result=1)

    assert check(reports) == [
        "backend: CVE-2026-116587 in github.com/go-jose/go-jose/v3 v3.0.5 (backend)",
        "backend: CVE-2026-116587 in github.com/go-jose/go-jose/v3 v3.0.5 (backend)",
        "web: CVE-2026-116587 in github.com/go-jose/go-jose/v3 v3.0.5 (web-1)",
    ]


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
@pytest.mark.parametrize("service", ["backend", "gateway"])
def test_mismatched_or_incomplete_scans_are_refused(
    reports: Path, service: str, change: str, message: str
) -> None:
    path = reports / f"{service}.json"
    report: dict[str, Any] = json.loads(path.read_text())
    if change == "image":
        report["Metadata"]["ImageID"] = "sha256:" + "b" * 64
    elif change == "identity":
        (reports / f"{service}.image").write_text(f"{service}:latest")
    elif change == "os":
        report["Metadata"]["OS"]["Family"] = ""
    elif change == "inventory":
        report["Results"] = []
    elif change == "packages":
        report["Results"][0]["Packages"] = []
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match=f"{service}: {message}"):
        check(reports)


@pytest.mark.parametrize("service", SERVICES)
def test_every_service_report_is_required(reports: Path, service: str) -> None:
    (reports / f"{service}.json").unlink()
    with pytest.raises(FileNotFoundError):
        check(reports)


def test_main_lists_every_finding_and_fails(
    reports: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    add_finding(reports, "web")
    add_finding(reports, "rabbitmq")
    monkeypatch.setattr("sys.argv", ["check_image_scans.py", str(reports)])

    with pytest.raises(SystemExit, match="Image scan gate failed: 2 high or critical findings"):
        check_image_scans.main()
    assert capsys.readouterr().out.splitlines() == [
        "web: CVE-2026-116587 in github.com/go-jose/go-jose/v3 v3.0.5 (web)",
        "rabbitmq: CVE-2026-116587 in github.com/go-jose/go-jose/v3 v3.0.5 (rabbitmq)",
    ]


def test_main_accepts_clean_scans(
    reports: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["check_image_scans.py", str(reports)])

    check_image_scans.main()

    assert capsys.readouterr().out.splitlines() == [
        f"{service}: HIGH/CRITICAL=0" for service in SERVICES
    ]


def test_main_refuses_an_unreadable_scan(reports: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (reports / "postgres.json").write_text("not json")
    monkeypatch.setattr("sys.argv", ["check_image_scans.py", str(reports)])

    with pytest.raises(SystemExit, match="Image scan gate failed: "):
        check_image_scans.main()
