import http.client
import json
import socket
import threading
from collections.abc import Iterator
from email.message import Message
from http.server import ThreadingHTTPServer

import pytest
from django.test import RequestFactory
from django.utils import translation

from pyracast import maintenance
from pyracast.api.errors import coded, error_json
from pyracast.errors import MESSAGES, ErrorCode


@pytest.fixture
def origin() -> Iterator[str]:
    server = maintenance.server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def fetch(
    url: str, accept: str = "*/*", headers: dict[str, str] | None = None
) -> tuple[int, Message, bytes]:
    origin, path = url.split("/", 1)
    connection = http.client.HTTPConnection(origin, timeout=5)
    try:
        connection.request("GET", f"/{path}", headers={"Accept": accept, **(headers or {})})
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


def test_a_navigation_gets_the_maintenance_page_with_a_retry_delay(origin: str) -> None:
    status, headers, body = fetch(f"{origin}/page", accept="text/html,*/*;q=0.8")

    assert status == 503
    assert headers["Retry-After"] == str(maintenance.RETRY_AFTER_SECONDS)
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert b"<title>Pyracast maintenance</title>" in body
    assert body.count(b"<section lang=") == 2


@pytest.mark.parametrize("accept", ["*/*", "application/json"])
def test_an_api_call_gets_the_api_error_body_with_a_retry_delay(origin: str, accept: str) -> None:
    status, headers, body = fetch(f"{origin}/api", accept=accept)

    assert status == 503
    assert headers["Retry-After"] == str(maintenance.RETRY_AFTER_SECONDS)
    assert headers["Content-Type"] == "application/json"
    assert headers["Cache-Control"] == "no-store"
    assert json.loads(body) == {
        "code": "maintenance",
        "message": maintenance.MESSAGES["en"],
        "retry_after": maintenance.RETRY_AFTER_SECONDS,
    }


def api_answer(headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    request = RequestFactory().get("/api/session/", headers=headers)
    response = error_json(
        request,
        lambda: coded(ErrorCode.MAINTENANCE, 503, retry_after=maintenance.RETRY_AFTER_SECONDS),
    )
    return response.status_code, dict(response.headers), response.content


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Accept-Language": "en"},
        {"Accept-Language": "tr-TR,tr;q=0.9,en;q=0.5"},
        {"Accept-Language": "de"},
        {"Accept-Language": "de", "CF-IPCountry": "TR"},
        {"Accept-Language": "tr;q=0", "CF-IPCountry": "TR"},
        {"Cookie": "pyracast_language=tr", "Accept-Language": "en"},
        {"Cookie": "pyracast_language=xx; other=1", "Accept-Language": "tr"},
    ],
)
def test_the_notice_answers_byte_for_byte_what_the_api_would(
    origin: str, headers: dict[str, str]
) -> None:
    expected_status, expected_headers, expected_body = api_answer(headers)

    status, notice_headers, body = fetch(f"{origin}/api", headers=headers)

    assert (status, body) == (expected_status, expected_body)
    assert notice_headers["Retry-After"] == expected_headers["Retry-After"]
    assert notice_headers["Content-Type"] == expected_headers["Content-Type"]
    assert "no-store" in expected_headers["Cache-Control"]


def test_the_notice_messages_are_the_catalogued_api_messages() -> None:
    for language, message in maintenance.MESSAGES.items():
        with translation.override(language):
            assert str(MESSAGES[ErrorCode.MAINTENANCE]) == message
    assert ErrorCode.MAINTENANCE.value == maintenance.CODE


def test_a_navigation_to_a_backend_page_gets_the_maintenance_page(origin: str) -> None:
    status, headers, body = fetch(f"{origin}/api", accept="text/html")

    assert status == 503
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert b"<title>Pyracast maintenance</title>" in body


def test_health_answers_ok_for_the_container_check(origin: str) -> None:
    status, _, body = fetch(f"{origin}/health")

    assert (status, body) == (200, b"ok")


def test_main_serves_on_the_container_address(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[tuple[str, int]] = []
    monkeypatch.setattr(socket, "gethostname", lambda: "maintenance")
    monkeypatch.setattr(socket, "gethostbyname", lambda name: "127.0.0.1")
    monkeypatch.setattr(
        ThreadingHTTPServer,
        "serve_forever",
        lambda self, poll_interval=0.5: served.append(self.server_address),
    )

    maintenance.main(port=0)

    assert served[0][0] == "127.0.0.1"
