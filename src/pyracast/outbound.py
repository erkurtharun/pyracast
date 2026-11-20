import time

import httpx

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
READ_DEADLINE_SECONDS = 30


class UnboundedResponseError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.too_large = reason == "too_large"


def read_bounded(response: httpx.Response, started: float) -> bytes:
    # httpx timeouts bound each socket read, not the whole body; a dripping or endless answer
    # would otherwise hold a worker past its lease.
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) > MAX_RESPONSE_BYTES:
            raise UnboundedResponseError("too_large")
        if time.monotonic() - started > READ_DEADLINE_SECONDS:
            raise UnboundedResponseError("deadline")
    return bytes(body)
