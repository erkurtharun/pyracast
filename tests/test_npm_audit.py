import json
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from scripts.npm_audit import (
    AuditException,
    audit_failures,
    load_exceptions,
    main,
    reported_advisories,
    run_audit,
)

TODAY = date(2026, 11, 17)
REPORT_ERRORS = (
    "npm audit failed|Unsupported npm audit report|has no causes|unknown cause|without an identity"
    "|leads to no advisory"
)
EXCEPTION_ERRORS = "must be a list|Malformed audit exception|Invalid isoformat"
PICOMATCH = {
    "source": 1001,
    "name": "picomatch",
    "url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc",
}
NEXT = {"source": 1002, "name": "next", "url": "https://github.com/advisories/GHSA-dddd-eeee-ffff/"}
REPORT: dict[str, Any] = {
    "auditReportVersion": 2,
    "vulnerabilities": {
        "picomatch": {"via": [PICOMATCH]},
        "micromatch": {"via": ["picomatch"]},
        "fast-glob": {"via": ["micromatch"]},
        "next": {"via": ["picomatch", NEXT]},
    },
}


def exception(
    advisory: str, *, expires: str = "2026-12-17", reason: str = "Tooling only."
) -> AuditException:
    return {"advisory": advisory, "package": "picomatch", "reason": reason, "expires": expires}


def report(**vulnerabilities: Any) -> dict[str, Any]:
    return {"auditReportVersion": 2, "vulnerabilities": vulnerabilities}


def test_every_advisory_is_read_from_direct_causes_only() -> None:
    assert reported_advisories(REPORT) == {"GHSA-aaaa-bbbb-cccc", "GHSA-dddd-eeee-ffff"}


def test_advisories_without_a_github_identity_keep_their_npm_source() -> None:
    first = {"source": 7, "url": "https://example.test/one/advisory"}
    second = {"source": 8, "url": "https://example.test/two/advisory"}
    assert reported_advisories(report(a={"via": [first]}, b={"via": [second]})) == {
        "npm-7",
        "npm-8",
    }


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"error": {"code": "ENOLOCK"}},
        {"vulnerabilities": {}},
        {"auditReportVersion": 1, "vulnerabilities": {}},
        {"auditReportVersion": 2, "vulnerabilities": []},
        report(a=None),
        report(a={}),
        report(a={"via": []}),
        report(a={"via": [None]}),
        report(a={"via": [7]}),
        report(a={"via": [{"url": "https://example.test/advisory"}]}),
        report(a={"via": [{"source": True, "url": "https://example.test/advisory"}]}),
        report(a={"via": [PICOMATCH]}, b={"via": ["missing"]}),
        report(a={"via": [PICOMATCH]}, b={"via": ["c"]}, c={"via": ["b"]}),
    ],
)
def test_a_report_npm_did_not_produce_as_expected_fails(bad: Any) -> None:
    with pytest.raises(ValueError, match=REPORT_ERRORS):
        reported_advisories(bad)


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
    reasonless = exception("GHSA-aaaa-bbbb-cccc", reason=" ")
    assert audit_failures({"GHSA-aaaa-bbbb-cccc"}, [reasonless], TODAY) == [
        "Audit exception GHSA-aaaa-bbbb-cccc has no reason.",
        "Advisory GHSA-aaaa-bbbb-cccc is not accepted.",
    ]


def test_an_exception_no_longer_reported_must_be_removed() -> None:
    assert audit_failures(set(), [exception("GHSA-aaaa-bbbb-cccc")], TODAY) == [
        "Audit exception GHSA-aaaa-bbbb-cccc is no longer reported; remove it."
    ]


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        "[1]",
        json.dumps([{"advisory": "GHSA-aaaa-bbbb-cccc", "reason": "x", "expires": "2026-12-17"}]),
        json.dumps([{**exception("GHSA-aaaa-bbbb-cccc"), "scope": "dev"}]),
        json.dumps([{**exception("GHSA-aaaa-bbbb-cccc"), "package": 1}]),
        json.dumps([exception("GHSA-aaaa-bbbb-cccc", expires="next month")]),
    ],
)
def test_a_malformed_exceptions_file_fails(text: str) -> None:
    with pytest.raises(ValueError, match=EXCEPTION_ERRORS):
        load_exceptions(text)


def test_the_committed_exceptions_load() -> None:
    web = Path(__file__).resolve().parents[1] / "web"
    for entry in load_exceptions((web / "audit-exceptions.json").read_text(encoding="utf-8")):
        assert entry["advisory"].startswith("GHSA-")
        assert entry["reason"].strip()


def fake_npm(
    monkeypatch: pytest.MonkeyPatch, stdout: str, calls: list[list[str]], *, status: int = 1
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, status, stdout=stdout, stderr="npm broke")

    monkeypatch.setattr(subprocess, "run", run)


@pytest.mark.parametrize(("status", "audit"), [(0, report()), (0, REPORT), (1, REPORT)])
def test_npm_reports_with_its_normal_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, audit: dict[str, Any]
) -> None:
    calls: list[list[str]] = []
    fake_npm(monkeypatch, json.dumps(audit), calls, status=status)
    assert run_audit(tmp_path) == audit
    assert calls == [["/bin/npm", "audit", "--json"]]


def test_a_failure_status_without_vulnerabilities_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_npm(monkeypatch, json.dumps(report()), [], status=1)
    with pytest.raises(RuntimeError, match="exited with 1, contradicting its report"):
        run_audit(tmp_path)


def test_any_other_npm_status_fails_even_with_a_parseable_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_npm(monkeypatch, json.dumps(REPORT), [], status=2)
    with pytest.raises(RuntimeError, match="npm audit exited with 2: npm broke"):
        run_audit(tmp_path)


def test_empty_npm_output_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_npm(monkeypatch, "", [], status=0)
    with pytest.raises(json.JSONDecodeError):
        run_audit(tmp_path)


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
    fake_npm(monkeypatch, json.dumps(REPORT), calls)
    assert main([str(tmp_path)]) == status
    assert calls == [["/bin/npm", "audit", "--json"]]
    assert capsys.readouterr().out == printed


def test_main_requires_npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "audit-exceptions.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        main([str(tmp_path)])
