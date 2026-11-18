import http.client
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.support.browser import Gateway


class Upstream(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, name: str) -> None:
        super().__init__(("127.0.0.1", 0), Answer)
        self.name = name
        self.requests: list[tuple[str, str, str, bytes]] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class Answer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: Upstream

    def log_message(self, *_args: object) -> None:
        pass

    def answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.requests.append((self.command, self.path, str(self.headers["Host"]), body))
        payload = self.server.name.encode()
        if self.path == "/redirect/":
            self.send_response(302)
            self.send_header("Location", "/app/")
        else:
            self.send_response(200)
        self.send_header("Set-Cookie", "first=1; Path=/")
        self.send_header("Set-Cookie", "second=2; Path=/")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self.answer()

    def do_POST(self) -> None:
        self.answer()


@contextmanager
def serving(server: ThreadingHTTPServer) -> Iterator[None]:
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


@pytest.fixture
def upstreams() -> Iterator[tuple[Upstream, Upstream, Gateway]]:
    backend, frontend = Upstream("backend"), Upstream("frontend")
    gateway = Gateway(backend.url, frontend.url)
    with serving(backend), serving(frontend), serving(gateway):
        yield backend, frontend, gateway


def request(
    gateway: Gateway, method: str, path: str, body: bytes | None = None
) -> http.client.HTTPResponse:
    connection = http.client.HTTPConnection("127.0.0.1", gateway.server_port, timeout=10)
    connection.request(method, path, body=body)
    response = connection.getresponse()
    response.read()
    connection.close()
    return response


@pytest.mark.parametrize(
    ("path", "service"),
    [
        ("/api/session/", "backend"),
        ("/api", "backend"),
        ("/api/auth/browser/v1/config", "backend"),
        ("/admin/", "backend"),
        ("/static/admin/site.css", "backend"),
        ("/health/ready/", "backend"),
        ("/app/", "frontend"),
        ("/", "frontend"),
        ("/administration/", "frontend"),
        ("/_next/static/chunk.js", "frontend"),
    ],
)
def test_the_browser_gateway_routes_each_path_as_production_does(
    upstreams: tuple[Upstream, Upstream, Gateway], path: str, service: str
) -> None:
    backend, frontend, gateway = upstreams

    response = request(gateway, "GET", path)

    assert response.status == 200
    assert [*backend.requests, *frontend.requests] == [
        ("GET", path, f"127.0.0.1:{gateway.server_port}", b"")
    ]
    assert {"backend": backend, "frontend": frontend}[service].requests


def test_the_browser_gateway_forwards_bodies_cookies_and_redirects_unchanged(
    upstreams: tuple[Upstream, Upstream, Gateway],
) -> None:
    backend, frontend, gateway = upstreams

    posted = request(gateway, "POST", "/api/auth/browser/v1/auth/login", b'{"email": "x"}')
    redirected = request(gateway, "GET", "/redirect/")

    assert posted.headers.get_all("Set-Cookie") == ["first=1; Path=/", "second=2; Path=/"]
    assert backend.requests[0][::3] == ("POST", b'{"email": "x"}')
    assert (redirected.status, redirected.headers["Location"]) == (302, "/app/")
    assert frontend.requests[0][:2] == ("GET", "/redirect/")
    assert gateway.origin == f"http://127.0.0.1:{gateway.server_port}"


def test_the_browser_gateway_answers_bad_gateway_while_an_upstream_is_down() -> None:
    backend = Upstream("backend")
    stopped = backend.url
    backend.server_close()
    frontend = Upstream("frontend")
    gateway = Gateway(stopped, frontend.url)
    with serving(frontend), serving(gateway):
        response = request(gateway, "GET", "/api/session/")

    assert response.status == 502
    assert frontend.requests == []
