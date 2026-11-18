import http.client
import json
import threading
from collections.abc import Iterator
from email.message import Message

import pytest

from pyracast import maintenance


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


def fetch(url: str, accept: str = "*/*", method: str = "GET") -> tuple[int, Message, bytes]:
    origin, path = url.split("/", 1)
    connection = http.client.HTTPConnection(origin, timeout=5)
    try:
        connection.request(method, f"/{path}", headers={"Accept": accept})
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
    assert json.loads(body) == {"code": "maintenance"}


def test_a_navigation_to_a_backend_page_gets_the_maintenance_page(origin: str) -> None:
    status, headers, body = fetch(f"{origin}/api", accept="text/html")

    assert status == 503
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert b"<title>Pyracast maintenance</title>" in body


def test_the_notice_answers_head_and_writes_without_a_body(origin: str) -> None:
    status, headers, body = fetch(f"{origin}/page", method="HEAD")
    posted, _, posted_body = fetch(f"{origin}/api", method="POST")

    assert (status, body) == (503, b"")
    assert headers["Retry-After"] == str(maintenance.RETRY_AFTER_SECONDS)
    assert posted == 503
    assert json.loads(posted_body) == {"code": "maintenance"}


def test_health_answers_ok_for_the_container_check(origin: str) -> None:
    status, _, body = fetch(f"{origin}/health")

    assert (status, body) == (200, b"ok")


def test_main_serves_on_the_container_address(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[tuple[str, int]] = []
    monkeypatch.setattr(maintenance.socket, "gethostname", lambda: "maintenance")
    monkeypatch.setattr(maintenance.socket, "gethostbyname", lambda name: "127.0.0.1")
    monkeypatch.setattr(
        maintenance.ThreadingHTTPServer,
        "serve_forever",
        lambda self, poll_interval=0.5: served.append(self.server_address),
    )

    maintenance.main(port=0)

    assert served[0][0] == "127.0.0.1"
