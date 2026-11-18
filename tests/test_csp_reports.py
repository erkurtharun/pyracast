import json
import logging
from collections.abc import Callable

import pytest
from django.conf import settings
from django.test import Client

from pyracast.api import csp_reports

URL = "/api/csp-reports/"


Logged = Callable[[], list[dict[str, str]]]


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Logged:
    monkeypatch.setattr(csp_reports.logger, "handlers", [caplog.handler])
    monkeypatch.setattr(csp_reports.logger, "propagate", False)
    caplog.set_level(logging.INFO, logger=csp_reports.logger.name)
    return lambda: [json.loads(record.getMessage()) for record in caplog.records]


def post(body: object, content_type: str = "application/csp-report") -> int:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return Client().post(URL, data, content_type=content_type).status_code


def test_both_policies_report_their_violations_to_the_endpoint() -> None:
    assert settings.SECURE_CSP["report-uri"] == [URL]


def test_a_report_uri_violation_is_logged_by_origin_and_first_path_segment(
    logged: Logged,
) -> None:
    report = {
        "csp-report": {
            "document-uri": "https://user:secret@pyracast.example/invitations/accept/?key=secret#x",
            "blocked-uri": "https://user:secret@cdn.example/script.js?token=secret",
            "effective-directive": "script-src-elem",
            "violated-directive": "script-src-elem",
            "disposition": "enforce",
            "script-sample": "alert('secret')",
        }
    }

    assert post(report) == 204
    assert logged() == [
        {
            "event": "csp_violation",
            "directive": "script-src-elem",
            "blocked": "https://cdn.example",
            "document": "/invitations/",
            "disposition": "enforce",
        }
    ]


@pytest.mark.parametrize(
    ("blocked", "origin"),
    [
        ("https://cdn.example:8443/x", "https://cdn.example:8443"),
        ("https://user:secret@CDN.example:8443/x", "https://cdn.example:8443"),
        ("https://[2001:db8::1]:8443/x", "https://[2001:db8::1]:8443"),
        ("wss://user@socket.example/x", "wss://socket.example"),
    ],
)
def test_a_blocked_url_is_logged_as_its_origin_without_credentials(
    logged: Logged, blocked: str, origin: str
) -> None:
    report = {"csp-report": {"effective-directive": "connect-src", "blocked-uri": blocked}}

    assert post(report) == 204
    assert [entry["blocked"] for entry in logged()] == [origin]


@pytest.mark.parametrize(
    ("document", "route"),
    [
        ("https://pyracast.example/", "/"),
        ("https://pyracast.example/app", "/app/"),
        ("https://pyracast.example/admin/regions/region/3/change/", "/admin/"),
        ("https://pyracast.example/reset/key/secret-token/", "/reset/"),
        ("about:blank", ""),
        ("https://[", ""),
        ("https://[x]/", ""),
    ],
)
def test_a_document_is_logged_by_its_first_path_segment(
    logged: Logged, document: str, route: str
) -> None:
    report = {"csp-report": {"effective-directive": "img-src", "document-uri": document}}

    assert post(report) == 204
    assert [entry["document"] for entry in logged()] == [route]


@pytest.mark.parametrize("url", ["https://[", "https://[x]/", "https://cdn.example:port/"])
def test_a_malformed_blocked_url_is_logged_as_unreadable(logged: Logged, url: str) -> None:
    report = {"csp-report": {"effective-directive": "img-src", "blocked-uri": url}}

    assert post(report) == 204
    assert [entry["blocked"] for entry in logged()] == [""]


def test_reporting_api_batches_log_only_their_csp_violations(logged: Logged) -> None:
    reports = [
        {
            "type": "csp-violation",
            "url": "https://pyracast.example/app/",
            "body": {
                "documentURL": "https://pyracast.example/app/?tab=1",
                "blockedURL": "inline",
                "effectiveDirective": "style-src-attr",
                "disposition": "report",
            },
        },
        {"type": "deprecation", "body": {"id": "x"}},
    ]

    assert post(reports, "application/reports+json") == 204
    assert logged() == [
        {
            "event": "csp_violation",
            "directive": "style-src-attr",
            "blocked": "inline",
            "document": "/app/",
            "disposition": "report",
        }
    ]


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"not json", "application/csp-report"),
        ({"csp-report": "x"}, "application/csp-report"),
        ([{"type": "csp-violation", "body": []}], "application/reports+json"),
        ({"csp-report": {"effective-directive": "script-src"}}, "application/json"),
        (b"{" + b" " * csp_reports.MAX_REPORT_BYTES + b"}", "application/csp-report"),
    ],
)
def test_unreadable_unexpected_or_oversized_reports_are_dropped(
    logged: Logged, body: object, content_type: str
) -> None:
    assert post(body, content_type) == 204
    assert logged() == []


def test_a_batch_logs_at_most_its_bounded_number_of_violations(logged: Logged) -> None:
    violation = {"type": "csp-violation", "body": {"effectiveDirective": "img-src"}}

    assert post([violation] * 50, "application/reports+json") == 204
    assert len(logged()) == csp_reports.MAX_REPORTS


def test_only_posted_reports_are_accepted() -> None:
    assert Client().get(URL).status_code == 405


@pytest.mark.parametrize(
    ("blocked", "logged_as"),
    [("data:image/png;base64,c2VjcmV0", "data"), ("eval", "eval"), ("x" * 40, "x" * 32)],
)
def test_a_blocked_resource_without_an_origin_is_logged_by_its_scheme_or_keyword(
    logged: Logged, blocked: str, logged_as: str
) -> None:
    report = {"csp-report": {"effective-directive": "img-src", "blocked-uri": blocked}}

    assert post(report) == 204
    assert [entry["blocked"] for entry in logged()] == [logged_as]


def test_a_report_needs_no_csrf_token_because_browsers_send_none() -> None:
    client = Client(enforce_csrf_checks=True)

    response = client.post(URL, b"{}", content_type="application/csp-report")

    assert response.status_code == 204
