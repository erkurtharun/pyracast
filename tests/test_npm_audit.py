import json
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from scripts.npm_audit import AuditException, audit_failures, main, reported_advisories

TODAY = date(2026, 11, 17)
REPORT: dict[str, Any] = {
    "auditReportVersion": 2,
    "vulnerabilities": {
        "picomatch": {
            "via": [
                {"name": "picomatch", "url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc"}
            ]
        },
        "micromatch": {"via": ["picomatch"]},
        "next": {
            "via": [{"name": "next", "url": "https://github.com/advisories/GHSA-dddd-eeee-ffff/"}]
        },
    },
}


def exception(
    advisory: str, *, expires: str = "2026-12-17", reason: str = "Tooling only."
) -> AuditException:
    return {"advisory": advisory, "package": "picomatch", "reason": reason, "expires": expires}


def test_every_advisory_is_read_from_direct_causes_only() -> None:
    assert reported_advisories(REPORT) == {"GHSA-aaaa-bbbb-cccc", "GHSA-dddd-eeee-ffff"}


def test_an_npm_error_report_is_a_failure_not_an_empty_audit() -> None:
    with pytest.raises(ValueError, match="npm audit failed"):
        reported_advisories({"error": {"code": "ENOLOCK"}})


def test_only_accepted_advisories_pass() -> None:
    found = {"GHSA-aaaa-bbbb-cccc", "GHSA-dddd-eeee-ffff"}
    assert audit_failures(found, [exception("GHSA-aaaa-bbbb-cccc")], TODAY) == [
        "Advisory GHSA-dddd-eeee-ffff is not accepted."
    ]
    assert audit_failures({"GHSA-aaaa-bbbb-cccc"}, [exception("GHSA-aaaa-bbbb-cccc")], TODAY) == []


def test_an_exception_counts_until_its_expiry_day_and_fails_after_it() -> None:
    found = {"GHSA-aaaa-bbbb-cccc"}
    on_the_day = exception("GHSA-aaaa-bbbb-cccc", expires="2026-11-17")
    assert audit_failures(found, [on_the_day], TODAY) == []
    expired = exception("GHSA-aaaa-bbbb-cccc", expires="2026-11-16")
    assert audit_failures(found, [expired], TODAY) == [
        "Audit exception GHSA-aaaa-bbbb-cccc expired on 2026-11-16.",
        "Advisory GHSA-aaaa-bbbb-cccc is not accepted.",
    ]


def test_an_exception_without_a_reason_accepts_nothing() -> None:
    assert audit_failures(
        {"GHSA-aaaa-bbbb-cccc"}, [exception("GHSA-aaaa-bbbb-cccc", reason=" ")], TODAY
    ) == [
        "Audit exception GHSA-aaaa-bbbb-cccc has no reason.",
        "Advisory GHSA-aaaa-bbbb-cccc is not accepted.",
    ]


def test_an_exception_no_longer_reported_must_be_removed() -> None:
    assert audit_failures(set(), [exception("GHSA-aaaa-bbbb-cccc")], TODAY) == [
        "Audit exception GHSA-aaaa-bbbb-cccc is no longer reported; remove it."
    ]


def test_the_committed_exceptions_are_complete_records() -> None:
    web = Path(__file__).resolve().parents[1] / "web"
    for entry in json.loads((web / "audit-exceptions.json").read_text(encoding="utf-8")):
        assert set(entry) == {"advisory", "package", "reason", "expires"}
        assert entry["advisory"].startswith("GHSA-")
        assert entry["reason"].strip()
        date.fromisoformat(entry["expires"])


def fake_npm(
    monkeypatch: pytest.MonkeyPatch, report: dict[str, Any], calls: list[list[str]]
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout=json.dumps(report), stderr="")

    monkeypatch.setattr(subprocess, "run", run)


@pytest.mark.parametrize(
    ("accepted", "status", "printed"),
    [
        (["GHSA-aaaa-bbbb-cccc", "GHSA-dddd-eeee-ffff"], 0, ""),
        (["GHSA-aaaa-bbbb-cccc"], 1, "Advisory GHSA-dddd-eeee-ffff is not accepted.\n"),
    ],
)
def test_main_audits_the_web_directory_against_its_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    accepted: list[str],
    status: int,
    printed: str,
) -> None:
    (tmp_path / "audit-exceptions.json").write_text(
        json.dumps([exception(advisory, expires="2999-01-01") for advisory in accepted]),
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    fake_npm(monkeypatch, REPORT, calls)
    assert main([str(tmp_path)]) == status
    assert calls == [["/bin/npm", "audit", "--json"]]
    assert capsys.readouterr().out == printed


def test_main_requires_npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "audit-exceptions.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        main([str(tmp_path)])
