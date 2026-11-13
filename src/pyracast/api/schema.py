from collections.abc import Callable
from typing import Any

type Endpoint = tuple[str, str, str, Callable[..., Any]]


def customer_endpoints(endpoints: list[Endpoint]) -> list[Endpoint]:
    return [endpoint for endpoint in endpoints if not endpoint[0].startswith("/api/platform/")]
