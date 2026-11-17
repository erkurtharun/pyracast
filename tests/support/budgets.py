from collections.abc import Callable, Iterator
from contextlib import contextmanager

from django.db import connections
from django.test.utils import CaptureQueriesContext

# Tests mirror `default` into the `auth_cache` alias, so only `default` is counted.
ALIAS = "default"


@contextmanager
def query_budget(max_queries: int) -> Iterator[CaptureQueriesContext]:
    with CaptureQueriesContext(connections[ALIAS]) as captured:
        yield captured
    statements = [query["sql"] for query in captured.captured_queries]
    assert len(statements) <= max_queries, (
        f"{len(statements)} queries exceed the budget of {max_queries}:\n" + "\n".join(statements)
    )


def count_queries(action: Callable[[], object]) -> int:
    action()
    with CaptureQueriesContext(connections[ALIAS]) as captured:
        action()
    return len(captured)


def assert_scale_invariant(
    seed: Callable[[int], None], action: Callable[[], object], *, small: int, large: int
) -> None:
    seed(small)
    small_count = count_queries(action)
    seed(large - small)
    large_count = count_queries(action)
    assert small_count == large_count, (
        f"queries grow with stored rows: {small_count} at {small}, {large_count} at {large}"
    )
