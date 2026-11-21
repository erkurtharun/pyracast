import http.client
import time
from collections.abc import Callable, Mapping

import pytest

from deploy import edge_gate

DOMAIN = "pyracast.example"
RELEASE = "a" * 40 + "-" + "b" * 12
ADDRESS = f"https://{DOMAIN}/health/live/?release={RELEASE}"
Answer = tuple[int, Mapping[str, str], bytes] | BaseException
ORIGIN: Answer = (200, {"content-type": "text/plain"}, b"ok")
CHALLENGE: Answer = (403, {"cf-mitigated": "challenge"}, b"<html>Just a moment...</html>")
EDGE_ERROR: Answer = (525, {"server": "cloudflare"}, b"<html>SSL handshake failed</html>")


def edge(
    *answers: Answer,
) -> tuple[Callable[[str], tuple[int, Mapping[str, str], bytes]], list[str]]:
    queue = list(answers)
    asked: list[str] = []

    def fetch(address: str) -> tuple[int, Mapping[str, str], bytes]:
        asked.append(address)
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return fetch, asked


def test_the_origins_answer_through_the_edge_passes_at_once() -> None:
    fetch, asked = edge(ORIGIN)
    sleeps: list[float] = []

    assert edge_gate.gate(DOMAIN, RELEASE, fetch, sleeps.append) is None
    assert asked == [ADDRESS]
    assert sleeps == []


def test_an_edge_error_is_asked_again_until_the_origin_answers() -> None:
    fetch, asked = edge(EDGE_ERROR, OSError("connection reset"), ORIGIN)
    sleeps: list[float] = []

    assert edge_gate.gate(DOMAIN, RELEASE, fetch, sleeps.append) is None
    assert len(asked) == 3
    assert sleeps == [edge_gate.INTERVAL_SECONDS] * 2


@pytest.mark.parametrize(
    ("answer", "observation"),
    [
        (EDGE_ERROR, "answered 525"),
        ((200, {}, b"maintenance"), "answered 200 without the origin's health answer"),
        (OSError("connection refused"), "could not be fetched (connection refused)"),
        (http.client.RemoteDisconnected("closed"), "could not be fetched (closed)"),
    ],
)
def test_an_edge_that_never_reaches_the_origin_fails_after_every_attempt(
    answer: Answer, observation: str
) -> None:
    fetch, asked = edge(answer)
    sleeps: list[float] = []

    assert edge_gate.gate(DOMAIN, RELEASE, fetch, sleeps.append) == observation
    assert len(asked) == edge_gate.ATTEMPTS
    assert len(sleeps) == edge_gate.ATTEMPTS - 1


@pytest.mark.parametrize(
    "challenge",
    [
        CHALLENGE,
        (403, {"cf-mitigated": "Challenge"}, b""),
        (403, {"cf-mitigated": " challenge "}, b""),
    ],
)
def test_a_challenge_of_the_host_ends_the_gate_at_once(challenge: Answer) -> None:
    fetch, asked = edge(challenge, ORIGIN)
    sleeps: list[float] = []

    assert edge_gate.gate(DOMAIN, RELEASE, fetch, sleeps.append) == edge_gate.CHALLENGE
    assert len(asked) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("answers", "status", "line"),
    [
        ((ORIGIN,), 0, f"https://{DOMAIN}/health/live/ answered through the edge."),
        (
            (EDGE_ERROR,),
            1,
            f"https://{DOMAIN}/health/live/ did not answer through the edge in "
            f"{edge_gate.ATTEMPTS} attempts: it answered 525.",
        ),
        (
            (CHALLENGE,),
            3,
            f"The edge answered https://{DOMAIN}/health/live/ with a Cloudflare challenge of "
            "this host instead of the origin's answer: add an edge rule that lets the host's "
            "health request through, or set origin_pull_ca to an empty value so that origin "
            "pulls are not required.",
        ),
    ],
)
def test_main_states_each_outcome_in_one_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: tuple[Answer, ...],
    status: int,
    line: str,
) -> None:
    fetch, _ = edge(*answers)
    monkeypatch.setattr(edge_gate, "fetch", fetch)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    assert edge_gate.main([DOMAIN, RELEASE]) == status

    output = capsys.readouterr()
    assert (output.out if status == 0 else output.err) == line + "\n"


@pytest.mark.parametrize(
    "arguments",
    [
        ["https://pyracast.example", RELEASE],
        [DOMAIN, "../release"],
        [DOMAIN, "a" * 40],
    ],
)
def test_main_refuses_an_address_or_release_it_cannot_name_safely(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        edge_gate.main(arguments)

    assert refused.value.code == 2


def test_an_edge_request_asks_once_for_an_uncached_answer_and_closes_its_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class Response:
        status = 403
        headers = http.client.HTTPMessage()

        def read(self, size: int) -> bytes:
            calls.append(("read", size))
            return b"challenge"

    Response.headers["CF-Mitigated"] = "challenge"

    class Connection:
        def __init__(self, host: str, *, timeout: float) -> None:
            calls.append(("connect", host, timeout))

        def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
            calls.append((method, path, headers))

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)

    assert edge_gate.fetch(ADDRESS) == (403, {"cf-mitigated": "challenge"}, b"challenge")
    assert calls == [
        ("connect", DOMAIN, edge_gate.TIMEOUT_SECONDS),
        (
            "GET",
            f"/health/live/?release={RELEASE}",
            {"Cache-Control": "no-cache", "User-Agent": "pyracast-edge-gate"},
        ),
        ("read", edge_gate.BODY_LIMIT),
        "close",
    ]
