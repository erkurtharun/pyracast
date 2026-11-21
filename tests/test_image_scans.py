import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from deploy import check_image_scans
from deploy.check_image_scans import SERVICES, ScanException, load_exceptions, review_findings
from deploy.check_image_scans import check_image_scans as check

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 21)
FINDING = {
    "VulnerabilityID": "CVE-2026-116587",
    "PkgName": "github.com/go-jose/go-jose/v3",
    "InstalledVersion": "v3.0.5",
}
OPENSSL = {
    "VulnerabilityID": "CVE-2026-124709",
    "PkgName": "libssl3t64",
    "InstalledVersion": "3.5.7-1~deb13u3",
}
EXCEPTION_ERRORS = "must be a list|Malformed image scan exception|Invalid isoformat"


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


@pytest.fixture
def exceptions_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "image-scan-exceptions.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(check_image_scans, "EXCEPTIONS", path)
    return path


def add_finding(
    reports: Path, service: str, result: int = 0, finding: dict[str, str] = FINDING
) -> None:
    path = reports / f"{service}.json"
    report = json.loads(path.read_text())
    while len(report["Results"]) <= result:
        report["Results"].append({"Target": f"{service}-{result}", "Packages": []})
    report["Results"][result].setdefault("Vulnerabilities", None)
    findings = report["Results"][result]["Vulnerabilities"] or []
    report["Results"][result]["Vulnerabilities"] = [*findings, finding]
    path.write_text(json.dumps(report))


def entry(
    service: str = "backend",
    vulnerability: str = "CVE-2026-124709",
    package: str = "libssl3t64",
    *,
    reason: str = "Not affected: no QUIC server.",
    expires: str = "2026-12-21",
) -> ScanException:
    return {
        "service": service,
        "vulnerability": vulnerability,
        "package": package,
        "reason": reason,
        "expires": expires,
    }


def lines(items: list[Any]) -> list[str]:
    return [str(item) for item in items]


def test_every_deployed_image_is_scanned() -> None:
    assert SERVICES == ("backend", "web", "gateway", "rabbitmq", "postgres")


def test_the_scan_reads_each_image_the_release_built_from_the_local_daemon_and_pulls_none() -> None:
    script = (ROOT / "deploy" / "scan-images.sh").read_text(encoding="utf-8")

    assert f"for service in {' '.join(SERVICES)}; do" in script
    assert "identity=$(docker image inspect \"$image\" --format '{{.Id}}')" in script
    assert "docker pull" not in script
    assert "rabbitmq" not in script.replace(f"for service in {' '.join(SERVICES)}; do", "")


def test_trivy_hides_no_finding_and_the_checker_reads_exceptions_from_the_mounted_deploy_tree() -> (
    None
):
    script = (ROOT / "deploy" / "scan-images.sh").read_text(encoding="utf-8")

    assert script.count("--ignorefile /dev/null") == 1
    assert "--ignore-unfixed=false" in script
    assert ".trivyignore" not in script
    assert not (ROOT / ".trivyignore").exists()
    assert '-v "$root/deploy:/source/deploy:ro"' in script
    assert check_image_scans.EXCEPTIONS.resolve() == ROOT / "deploy" / "image-scan-exceptions.json"


def test_complete_clean_reports_have_no_findings(reports: Path) -> None:
    assert check(reports) == []


@pytest.mark.parametrize("service", SERVICES)
def test_any_high_or_critical_finding_is_reported(reports: Path, service: str) -> None:
    add_finding(reports, service)

    assert lines(check(reports)) == [
        f"{service}: CVE-2026-116587 in github.com/go-jose/go-jose/v3 v3.0.5 ({service})"
    ]


def test_every_finding_of_every_result_is_reported(reports: Path) -> None:
    add_finding(reports, "backend")
    add_finding(reports, "backend")
    add_finding(reports, "web", result=1)

    assert lines(check(reports)) == [
        "backend: CVE-2026-116587 in github.com/go-jose/go-jose/v3 v3.0.5 (backend)",
        "backend: CVE-2026-116587 in github.com/go-jose/go-jose/v3 v3.0.5 (backend)",
        "web: CVE-2026-116587 in github.com/go-jose/go-jose/v3 v3.0.5 (web-1)",
    ]


def test_a_matching_dated_exception_accepts_every_finding_it_names(reports: Path) -> None:
    add_finding(reports, "backend", finding=OPENSSL)
    add_finding(reports, "backend", result=1, finding=OPENSSL)
    add_finding(reports, "web", finding=OPENSSL)
    findings = check(reports)

    review = review_findings(findings, [entry(), entry("web")], TODAY)

    assert review.accepted == [(finding, "2026-12-21") for finding in findings]
    assert review.rejected == []
    assert review.problems == []


def test_an_exception_accepts_nothing_but_its_own_findings(reports: Path) -> None:
    add_finding(reports, "backend", finding=OPENSSL)
    add_finding(reports, "backend")
    add_finding(reports, "web", finding=OPENSSL)
    backend, other, web = check(reports)

    review = review_findings([backend, other, web], [entry()], TODAY)

    assert review.accepted == [(backend, "2026-12-21")]
    assert review.rejected == [other, web]
    assert review.problems == []


@pytest.mark.parametrize(
    "exception",
    [
        entry("web"),
        entry(package="libssl3"),
        entry(vulnerability="CVE-2026-124710"),
        entry(vulnerability="cve-2026-124709"),
        entry(package="Libssl3t64"),
    ],
    ids=[
        "another service",
        "another package",
        "another vulnerability",
        "another vulnerability case",
        "another package case",
    ],
)
def test_an_exception_for_another_service_package_or_vulnerability_accepts_nothing(
    reports: Path, exception: ScanException
) -> None:
    add_finding(reports, "backend", finding=OPENSSL)
    findings = check(reports)

    review = review_findings(findings, [exception], TODAY)

    assert review.accepted == []
    assert review.rejected == findings
    assert review.problems == [
        f"Image scan exception {exception['service']}: {exception['vulnerability']} in "
        f"{exception['package']} is no longer reported; remove it."
    ]


@pytest.mark.parametrize(
    ("today", "accepted"), [(date(2026, 12, 21), True), (date(2026, 12, 22), False)]
)
def test_an_exception_counts_until_its_expiry_day_and_fails_after_it(
    reports: Path, today: date, accepted: bool
) -> None:
    add_finding(reports, "backend", finding=OPENSSL)
    findings = check(reports)

    review = review_findings(findings, [entry()], today)

    assert review.accepted == ([(findings[0], "2026-12-21")] if accepted else [])
    assert review.rejected == ([] if accepted else findings)
    assert review.problems == (
        []
        if accepted
        else ["Image scan exception backend: CVE-2026-124709 in libssl3t64 expired on 2026-12-21."]
    )


@pytest.mark.parametrize("reason", ["", "  "])
def test_an_exception_without_a_reason_accepts_nothing(reports: Path, reason: str) -> None:
    add_finding(reports, "backend", finding=OPENSSL)
    findings = check(reports)

    review = review_findings(findings, [entry(reason=reason)], TODAY)

    assert review.accepted == []
    assert review.rejected == findings
    assert review.problems == [
        "Image scan exception backend: CVE-2026-124709 in libssl3t64 has no reason."
    ]


def test_an_exception_no_longer_reported_must_be_removed(reports: Path) -> None:
    review = review_findings(check(reports), [entry(), entry("web")], TODAY)

    assert review.accepted == []
    assert review.rejected == []
    assert review.problems == [
        "Image scan exception backend: CVE-2026-124709 in libssl3t64 is no longer reported; "
        "remove it.",
        "Image scan exception web: CVE-2026-124709 in libssl3t64 is no longer reported; remove it.",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        "[1]",
        json.dumps([{**entry(), "version": "3.5.7-1~deb13u3"}]),
        json.dumps([{key: value for key, value in entry().items() if key != "reason"}]),
        json.dumps([{**entry(), "expires": 20261221}]),
        json.dumps([{**entry(), "service": None}]),
        json.dumps([entry(expires="21.12.2026")]),
        json.dumps([entry(expires="")]),
        json.dumps([entry(expires="20261221")]),
        json.dumps([entry(expires="2026-W52-1")]),
        json.dumps([entry("api")]),
        json.dumps([entry("*")]),
        json.dumps([entry(vulnerability="")]),
        json.dumps([entry(vulnerability=" ")]),
        json.dumps([entry(package="")]),
        json.dumps([entry(package="\t")]),
    ],
    ids=[
        "not a list",
        "not an object",
        "unknown key",
        "missing key",
        "non-string date",
        "non-string service",
        "bad date",
        "empty date",
        "basic date",
        "week date",
        "unknown service",
        "wildcard service",
        "empty vulnerability",
        "blank vulnerability",
        "empty package",
        "blank package",
    ],
)
def test_a_malformed_exceptions_file_fails(text: str) -> None:
    with pytest.raises(ValueError, match=EXCEPTION_ERRORS):
        load_exceptions(text)


@pytest.mark.parametrize(
    "pair",
    [
        [entry(reason="Not affected: first."), entry(reason="Not affected: second.")],
        [entry(), entry(expires="2026-11-01")],
        [entry(expires="2026-11-01"), entry()],
        [entry(), entry(reason=" ")],
        [entry(reason=" "), entry()],
    ],
    ids=[
        "two valid",
        "valid then expired",
        "expired then valid",
        "valid then reasonless",
        "reasonless then valid",
    ],
)
def test_two_exceptions_for_one_finding_are_refused(pair: list[ScanException]) -> None:
    with pytest.raises(
        ValueError,
        match=r"^Duplicate image scan exception: backend: CVE-2026-124709 in libssl3t64$",
    ):
        load_exceptions(json.dumps(pair))


def test_the_tracked_exceptions_are_exactly_the_two_reviewed_entries() -> None:
    tracked = load_exceptions(
        (ROOT / "deploy" / "image-scan-exceptions.json").read_text(encoding="utf-8")
    )

    assert tracked == [
        {
            "service": "backend",
            "vulnerability": "CVE-2026-124709",
            "package": "libssl3t64",
            "reason": "Not affected: the flaw is in OpenSSL's QUIC server, and this image runs "
            "none, because CPython's ssl module offers no QUIC and every process either serves "
            "plain HTTP over TCP behind the gateway or only opens outbound connections.",
            "expires": "2026-12-21",
        },
        {
            "service": "web",
            "vulnerability": "CVE-2026-124709",
            "package": "libssl3t64",
            "reason": "Not affected: the flaw is in OpenSSL's QUIC server, and this image runs "
            "none and never loads the system library, because the official Node.js binary "
            "carries its own OpenSSL and the Next.js server serves plain HTTP over TCP behind "
            "the gateway.",
            "expires": "2026-12-21",
        },
    ]


def test_main_lists_every_finding_and_fails(
    reports: Path,
    exceptions_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_finding(reports, "web")
    add_finding(reports, "rabbitmq")
    monkeypatch.setattr("sys.argv", ["check_image_scans.py", str(reports)])

    with pytest.raises(
        SystemExit,
        match="Image scan gate failed: 2 high or critical findings and 0 exception problems",
    ):
        check_image_scans.main()
    assert capsys.readouterr().out.splitlines() == [
        "web: CVE-2026-116587 in github.com/go-jose/go-jose/v3 v3.0.5 (web)",
        "rabbitmq: CVE-2026-116587 in github.com/go-jose/go-jose/v3 v3.0.5 (rabbitmq)",
    ]


def test_main_accepts_clean_scans(
    reports: Path,
    exceptions_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.argv", ["check_image_scans.py", str(reports)])

    check_image_scans.main()

    assert capsys.readouterr().out.splitlines() == [
        f"{service}: HIGH/CRITICAL=0, accepted=0" for service in SERVICES
    ]


def test_main_passes_accepted_findings_and_shows_them_with_their_end_date(
    reports: Path,
    exceptions_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expires = (datetime.now(UTC).date() + timedelta(days=30)).isoformat()
    exceptions_file.write_text(
        json.dumps([entry(expires=expires), entry("web", expires=expires)]), encoding="utf-8"
    )
    add_finding(reports, "backend", finding=OPENSSL)
    add_finding(reports, "web", finding=OPENSSL)
    monkeypatch.setattr("sys.argv", ["check_image_scans.py", str(reports)])

    check_image_scans.main()

    assert capsys.readouterr().out.splitlines() == [
        "backend: CVE-2026-124709 in libssl3t64 3.5.7-1~deb13u3 (backend) accepted until "
        f"{expires}",
        f"web: CVE-2026-124709 in libssl3t64 3.5.7-1~deb13u3 (web) accepted until {expires}",
        "backend: HIGH/CRITICAL=1, accepted=1",
        "web: HIGH/CRITICAL=1, accepted=1",
        "gateway: HIGH/CRITICAL=0, accepted=0",
        "rabbitmq: HIGH/CRITICAL=0, accepted=0",
        "postgres: HIGH/CRITICAL=0, accepted=0",
    ]


def test_main_shows_accepted_findings_and_fails_on_an_expired_or_unneeded_exception(
    reports: Path,
    exceptions_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    today = datetime.now(UTC).date()
    exceptions_file.write_text(
        json.dumps(
            [
                entry(expires=(today + timedelta(days=30)).isoformat()),
                entry("web", expires=(today - timedelta(days=1)).isoformat()),
                entry("gateway", expires=today.isoformat()),
            ]
        ),
        encoding="utf-8",
    )
    add_finding(reports, "backend", finding=OPENSSL)
    add_finding(reports, "web", finding=OPENSSL)
    monkeypatch.setattr("sys.argv", ["check_image_scans.py", str(reports)])

    with pytest.raises(
        SystemExit,
        match="Image scan gate failed: 1 high or critical findings and 2 exception problems",
    ):
        check_image_scans.main()
    assert capsys.readouterr().out.splitlines() == [
        "backend: CVE-2026-124709 in libssl3t64 3.5.7-1~deb13u3 (backend) accepted until "
        f"{(today + timedelta(days=30)).isoformat()}",
        "web: CVE-2026-124709 in libssl3t64 3.5.7-1~deb13u3 (web)",
        "Image scan exception web: CVE-2026-124709 in libssl3t64 expired on "
        f"{(today - timedelta(days=1)).isoformat()}.",
        "Image scan exception gateway: CVE-2026-124709 in libssl3t64 is no longer reported; "
        "remove it.",
    ]


def test_main_fails_on_an_unneeded_exception_when_every_finding_is_accepted(
    reports: Path,
    exceptions_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expires = (datetime.now(UTC).date() + timedelta(days=30)).isoformat()
    exceptions_file.write_text(
        json.dumps([entry(expires=expires), entry("web", expires=expires)]), encoding="utf-8"
    )
    add_finding(reports, "backend", finding=OPENSSL)
    monkeypatch.setattr("sys.argv", ["check_image_scans.py", str(reports)])

    with pytest.raises(
        SystemExit,
        match=r"^Image scan gate failed: 0 high or critical findings and 1 exception problems\.$",
    ):
        check_image_scans.main()
    assert capsys.readouterr().out.splitlines() == [
        "backend: CVE-2026-124709 in libssl3t64 3.5.7-1~deb13u3 (backend) accepted until "
        f"{expires}",
        "Image scan exception web: CVE-2026-124709 in libssl3t64 is no longer reported; remove it.",
    ]


def test_main_fails_on_two_exceptions_for_one_finding(
    reports: Path,
    exceptions_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expires = (datetime.now(UTC).date() + timedelta(days=30)).isoformat()
    exceptions_file.write_text(
        json.dumps([entry(expires=expires), entry(expires=expires)]), encoding="utf-8"
    )
    add_finding(reports, "backend", finding=OPENSSL)
    monkeypatch.setattr("sys.argv", ["check_image_scans.py", str(reports)])

    with pytest.raises(
        SystemExit,
        match=r"^Image scan gate failed: Duplicate image scan exception: backend: "
        r"CVE-2026-124709 in libssl3t64$",
    ):
        check_image_scans.main()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("field", ["VulnerabilityID", "PkgName"])
def test_main_refuses_a_finding_without_its_id_or_package(
    reports: Path, exceptions_file: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    add_finding(
        reports, "backend", finding={key: value for key, value in OPENSSL.items() if key != field}
    )
    monkeypatch.setattr("sys.argv", ["check_image_scans.py", str(reports)])

    with pytest.raises(SystemExit, match=f"^Image scan gate failed: '{field}'$"):
        check_image_scans.main()


@pytest.mark.parametrize("field", ["VulnerabilityID", "PkgName"])
def test_main_reports_a_finding_with_an_empty_id_or_package(
    reports: Path,
    exceptions_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
) -> None:
    add_finding(reports, "backend", finding={**OPENSSL, field: ""})
    monkeypatch.setattr("sys.argv", ["check_image_scans.py", str(reports)])

    with pytest.raises(
        SystemExit,
        match=r"^Image scan gate failed: 1 high or critical findings and 0 exception problems\.$",
    ):
        check_image_scans.main()
    assert capsys.readouterr().out.splitlines() == [
        "backend: CVE-2026-124709 in  3.5.7-1~deb13u3 (backend)"
        if field == "PkgName"
        else "backend:  in libssl3t64 3.5.7-1~deb13u3 (backend)"
    ]


@pytest.mark.parametrize(
    ("field", "key"), [("VulnerabilityID", "vulnerability"), ("PkgName", "package")]
)
def test_main_lets_no_exception_accept_a_finding_with_an_empty_id_or_package(
    reports: Path,
    exceptions_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    key: str,
) -> None:
    expires = (datetime.now(UTC).date() + timedelta(days=30)).isoformat()
    exceptions_file.write_text(json.dumps([{**entry(expires=expires), key: ""}]), encoding="utf-8")
    add_finding(reports, "backend", finding={**OPENSSL, field: ""})
    monkeypatch.setattr("sys.argv", ["check_image_scans.py", str(reports)])

    with pytest.raises(
        SystemExit, match=r"^Image scan gate failed: Malformed image scan exception"
    ):
        check_image_scans.main()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("text", [None, "not json", "{}", json.dumps([{**entry(), "reason": 1}])])
def test_main_fails_without_a_readable_well_formed_exceptions_file(
    reports: Path, exceptions_file: Path, monkeypatch: pytest.MonkeyPatch, text: str | None
) -> None:
    if text is None:
        exceptions_file.unlink()
    else:
        exceptions_file.write_text(text, encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["check_image_scans.py", str(reports)])

    with pytest.raises(SystemExit, match="Image scan gate failed: "):
        check_image_scans.main()


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
@pytest.mark.parametrize("service", SERVICES)
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


def test_main_refuses_an_unreadable_scan(
    reports: Path, exceptions_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (reports / "postgres.json").write_text("not json")
    monkeypatch.setattr("sys.argv", ["check_image_scans.py", str(reports)])

    with pytest.raises(SystemExit, match="Image scan gate failed: "):
        check_image_scans.main()
