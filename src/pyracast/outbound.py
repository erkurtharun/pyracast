import time

import httpx

from pyracast.background.deadlines import (
    HTTP_BODY_SECONDS,
    HTTP_CONNECT_SECONDS,
    HTTP_READ_SECONDS,
)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TIMEOUT = httpx.Timeout(HTTP_READ_SECONDS, connect=HTTP_CONNECT_SECONDS)


class UnboundedResponseError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.too_large = reason == "too_large"


def read_bounded(
    response: httpx.Response, started: float, *, limit: int = MAX_RESPONSE_BYTES
) -> bytes:
    # httpx timeouts bound each socket read, not the whole body; a dripping or endless answer
    # would otherwise hold a worker past its lease.
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) > limit:
            raise UnboundedResponseError("too_large")
        if time.monotonic() - started > HTTP_BODY_SECONDS:
            raise UnboundedResponseError("deadline")
    return bytes(body)
