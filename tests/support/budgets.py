from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager

from django.db import connections
from django.test.utils import CaptureQueriesContext

ALIASES = ("default", "auth_cache")


@contextmanager
def captured_statements() -> Iterator[list[str]]:
    statements: list[str] = []
    with ExitStack() as stack:
        captures = [
            stack.enter_context(CaptureQueriesContext(connections[alias])) for alias in ALIASES
        ]
        yield statements
    statements.extend(query["sql"] for capture in captures for query in capture.captured_queries)


@contextmanager
def query_budget(max_queries: int) -> Iterator[list[str]]:
    with captured_statements() as statements:
        yield statements
    assert len(statements) <= max_queries, (
        f"{len(statements)} queries exceed the budget of {max_queries}:\n" + "\n".join(statements)
    )


def count_queries(action: Callable[[], object]) -> int:
    action()
    with captured_statements() as statements:
        action()
    return len(statements)


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
