import argparse
import http.client
import json
import re
import sys
import time
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

ORIGIN = re.compile(r"https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
SESSION_KEYS = {"user", "csrf_token", "language", "expires_at", "session_tag"}
ATTEMPTS = 6
RETRY_SECONDS = 10
Fetch = Callable[[str], tuple[int, bytes]]


def public_origin(value: str) -> str:
    if ORIGIN.fullmatch(value) is None:
        raise ValueError(
            f"PYRACAST_PUBLIC_ORIGIN must be https:// and a lowercase host name, got {value!r}."
        )
    return value


def fetch(url: str) -> tuple[int, bytes]:
    parts = urlsplit(url)
    connection = http.client.HTTPSConnection(parts.netloc, timeout=15)
    try:
        connection.request(
            "GET",
            parts.path,
            headers={"User-Agent": "pyracast-release-probe", "Cache-Control": "no-cache"},
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _anonymous_session(body: bytes) -> bool:
    try:
        session = json.loads(body)
    except ValueError:
        return False
    return isinstance(session, dict) and set(session) == SESSION_KEYS and session["user"] is None


def _sign_in_page(body: bytes) -> bool:
    return b"<title>Pyracast workspace" in body


CHECKS: tuple[tuple[str, Callable[[bytes], bool], str], ...] = (
    ("/api/session/", _anonymous_session, "is not the session"),
    ("/app/", _sign_in_page, "is not the sign-in page"),
)


def _problem(
    origin: str, path: str, valid: Callable[[bytes], bool], wrong: str, get: Fetch
) -> str | None:
    try:
        status, body = get(origin + path)
    except (OSError, http.client.HTTPException) as error:
        return f"{path} could not be fetched: {error}"
    if status != 200:
        return f"{path} answered {status}"
    return None if valid(body) else f"{path} {wrong}"


def probe(origin: str, get: Fetch, sleep: Callable[[float], None]) -> list[str]:
    problems = []
    for path, valid, wrong in CHECKS:
        for attempt in range(ATTEMPTS):
            if attempt:
                sleep(RETRY_SECONDS)
            problem = _problem(origin, path, valid, wrong, get)
            if problem is None:
                break
        if problem is not None:
            problems.append(problem)
    return problems


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("origin")
    options = parser.parse_args(arguments)
    if not options.origin:
        print("::warning::PYRACAST_PUBLIC_ORIGIN is not set; the public origin was not probed.")
        return 0
    try:
        origin = public_origin(options.origin)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    problems = probe(origin, fetch, time.sleep)
    if problems:
        print(f"The public origin failed its probe: {'; '.join(problems)}", file=sys.stderr)
        return 1
    print(f"{origin} serves the session and sign-in page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
