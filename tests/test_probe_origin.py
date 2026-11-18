import json
import time
from collections.abc import Callable

import pytest

from deploy import probe_origin

SESSION = json.dumps(
    {"user": None, "csrf_token": "token", "language": "tr", "expires_at": None}
).encode()
APP = b"<!doctype html><html><head><title>Pyracast workspace</title></head></html>"


def site(answers: dict[str, list[tuple[int, bytes]]]) -> Callable[[str], tuple[int, bytes]]:
    def fetch(url: str) -> tuple[int, bytes]:
        queue = answers[url]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return fetch


def healthy() -> dict[str, list[tuple[int, bytes]]]:
    return {
        "https://pyracast.example/api/session/": [(200, SESSION)],
        "https://pyracast.example/app/": [(200, APP)],
    }


@pytest.mark.parametrize(
    "origin",
    ["https://pyracast.example", "https://forecast.pyracast.example"],
)
def test_a_public_https_origin_is_accepted(origin: str) -> None:
    assert probe_origin.public_origin(origin) == origin


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "http://pyracast.example",
        "https://pyracast.example/",
        "https://pyracast.example/app/",
        "https://pyracast.example:8443",
        "https://user@pyracast.example",
        "https://PYRACAST.example",
        "https://pyracast.example?x=1",
    ],
)
def test_any_other_origin_is_refused(origin: str) -> None:
    with pytest.raises(ValueError, match="PYRACAST_PUBLIC_ORIGIN"):
        probe_origin.public_origin(origin)


def test_a_healthy_origin_answers_the_session_and_the_sign_in_page() -> None:
    sleeps: list[float] = []

    assert probe_origin.probe("https://pyracast.example", site(healthy()), sleeps.append) == []
    assert sleeps == []


def test_a_slow_origin_is_asked_again_within_the_budget() -> None:
    answers = healthy()
    answers["https://pyracast.example/api/session/"] = [(502, b""), (200, SESSION)]
    sleeps: list[float] = []

    assert probe_origin.probe("https://pyracast.example", site(answers), sleeps.append) == []
    assert sleeps == [probe_origin.RETRY_SECONDS]


@pytest.mark.parametrize(
    ("path", "answer", "problem"),
    [
        ("/api/session/", (503, b""), "/api/session/ answered 503"),
        ("/api/session/", (200, b"<html>challenge</html>"), "/api/session/ is not the session"),
        (
            "/api/session/",
            (200, json.dumps({"user": {"id": 1}, "csrf_token": "t"}).encode()),
            "/api/session/ is not the session",
        ),
        ("/app/", (200, b"<html>challenge</html>"), "/app/ is not the sign-in page"),
        ("/app/", (403, b""), "/app/ answered 403"),
    ],
)
def test_a_wrong_answer_after_every_attempt_is_a_problem(
    path: str, answer: tuple[int, bytes], problem: str
) -> None:
    answers = healthy()
    answers[f"https://pyracast.example{path}"] = [answer]
    sleeps: list[float] = []

    assert probe_origin.probe("https://pyracast.example", site(answers), sleeps.append) == [problem]
    assert sleeps == [probe_origin.RETRY_SECONDS] * (probe_origin.ATTEMPTS - 1)


def test_an_unreachable_origin_is_a_problem() -> None:
    def fetch(url: str) -> tuple[int, bytes]:
        raise OSError("connection refused")

    problems = probe_origin.probe("https://pyracast.example", fetch, lambda _seconds: None)

    assert problems == [
        "/api/session/ could not be fetched: connection refused",
        "/app/ could not be fetched: connection refused",
    ]


def test_main_passes_a_healthy_origin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(probe_origin, "fetch", site(healthy()))

    assert probe_origin.main(["https://pyracast.example"]) == 0
    output = capsys.readouterr().out
    assert output == "https://pyracast.example serves the session and sign-in page.\n"


def test_main_fails_a_broken_origin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = healthy()
    answers["https://pyracast.example/app/"] = [(500, b"")]
    monkeypatch.setattr(probe_origin, "fetch", site(answers))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    assert probe_origin.main(["https://pyracast.example"]) == 1
    assert capsys.readouterr().err == "The public origin failed its probe: /app/ answered 500\n"


def test_main_warns_that_an_unset_origin_was_not_probed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fetch(url: str) -> tuple[int, bytes]:
        raise AssertionError(url)

    monkeypatch.setattr(probe_origin, "fetch", fetch)

    assert probe_origin.main([""]) == 0
    assert capsys.readouterr().out == (
        "::warning::PYRACAST_PUBLIC_ORIGIN is not set; the public origin was not probed.\n"
    )


def test_main_refuses_an_invalid_origin(capsys: pytest.CaptureFixture[str]) -> None:
    assert probe_origin.main(["http://pyracast.example"]) == 2
    assert "PYRACAST_PUBLIC_ORIGIN" in capsys.readouterr().err
