import pytest

from pyracast.plants import numerics
from tests.support.fit_reference import (
    digest,
    executed_lines,
    exhaustive_fit,
    fit_days,
    random_rows,
)

PINNED = {
    10: "fe6a8afcd8e71ea7d8053e554e724b59cbe3f1ff69516940bfd3cf9478e42507",
    20: "fc9743a5e4672cdcd5aa01c31024635c7d7e019d7f61f07e4464c51613ad0dbf",
    40: "58d6eaac159172533e6c506b1e7678aa21cdd720689d99be18d5df3f7efd4bf2",
    80: "a843074a3bad76d787043e838c151db67eb7c35d3e1806f1a7fa11dafc0a34ca",
}


@pytest.mark.parametrize("days", sorted(PINNED))
def test_the_fit_report_is_pinned_bit_for_bit(days: int) -> None:
    assert digest(fit_days(days)) == PINNED[days]


@pytest.mark.parametrize("seed", range(120))
def test_the_fit_equals_the_exhaustive_search_exactly(seed: int) -> None:
    rows, normalization, cap = random_rows(seed, 60)

    assert numerics._fit(rows, normalization, cap) == exhaustive_fit(rows, normalization, cap)


@pytest.mark.parametrize("days", [10, 40])
def test_doubling_the_input_at_most_two_and_a_half_times_the_fit_work(days: int) -> None:
    single, _ = executed_lines(lambda: fit_days(days))
    double, _ = executed_lines(lambda: fit_days(2 * days))

    assert double / single <= 2.5
