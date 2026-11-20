import argparse
import http.client
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from urllib.parse import urlsplit

PATH = "/health/live/"
ATTEMPTS = 12
INTERVAL_SECONDS = 5.0
TIMEOUT_SECONDS = 10.0
BODY_LIMIT = 4096
CHALLENGE = "challenge"
DOMAIN = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}")
RELEASE = re.compile(r"[0-9a-f]{40}-[0-9a-f]{12}")
Answer = tuple[int, Mapping[str, str], bytes]
Fetch = Callable[[str], Answer]


def fetch(address: str) -> Answer:
    parts = urlsplit(address)
    connection = http.client.HTTPSConnection(parts.netloc, timeout=TIMEOUT_SECONDS)
    try:
        connection.request(
            "GET",
            f"{parts.path}?{parts.query}",
            headers={"Cache-Control": "no-cache", "User-Agent": "pyracast-edge-gate"},
        )
        response = connection.getresponse()
        headers = {name.lower(): value for name, value in response.headers.items()}
        return response.status, headers, response.read(BODY_LIMIT)
    finally:
        connection.close()


def observe(get: Fetch, address: str) -> str | None:
    try:
        status, headers, body = get(address)
    except (OSError, http.client.HTTPException) as error:
        return f"could not be fetched ({error})"
    # Cloudflare marks its challenge pages with this header; the origin never sends it.
    if headers.get("cf-mitigated") == CHALLENGE:
        return CHALLENGE
    if status != 200:
        return f"answered {status}"
    if body.strip() != b"ok":
        return "answered 200 without the origin's health answer"
    return None


def gate(
    domain: str,
    release: str,
    get: Fetch,
    sleep: Callable[[float], None],
    attempts: int = ATTEMPTS,
) -> str | None:
    address = f"https://{domain}{PATH}?release={release}"
    observation = observe(get, address)
    for _ in range(attempts - 1):
        if observation in (None, CHALLENGE):
            break
        sleep(INTERVAL_SECONDS)
        observation = observe(get, address)
    return observation


def _matching(pattern: re.Pattern[str]) -> Callable[[str], str]:
    def parse(value: str) -> str:
        if pattern.fullmatch(value) is None:
            raise argparse.ArgumentTypeError(f"invalid value {value!r}")
        return value

    return parse


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Request the site's liveness through the public edge, as visitors reach it."
    )
    parser.add_argument("domain", type=_matching(DOMAIN))
    parser.add_argument("release", type=_matching(RELEASE))
    options = parser.parse_args(arguments)
    url = f"https://{options.domain}{PATH}"
    observation = gate(options.domain, options.release, fetch, time.sleep)
    if observation is None:
        print(f"{url} answered through the edge.")
        return 0
    if observation == CHALLENGE:
        print(
            f"The edge answered {url} with a Cloudflare challenge of this host instead of the "
            "origin's answer: add an edge rule that lets the host's health request through, or "
            "set origin_pull_ca to an empty value so that origin pulls are not required.",
            file=sys.stderr,
        )
        return 3
    print(
        f"{url} did not answer through the edge in {ATTEMPTS} attempts: it {observation}.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
