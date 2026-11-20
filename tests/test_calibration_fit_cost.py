import math
from dataclasses import replace

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
    10: "a1efccac2587ce19a3659d49f5ce9f7b3de5ef0c9de24496235186a019674ab8",
    20: "53a467e67d9b338bfa8f645d734bcc509e786cdbded02d4fd16784c963620ff5",
    40: "2c91f52471e2ad0ce2fc472449504e2014970867c3cbfb281116fb194d06d9b0",
    80: "35e823d8c222aeebe6c47e92432be443bb318913bebab81eafc04f313a98e470",
}


@pytest.mark.parametrize("days", sorted(PINNED))
def test_the_fit_report_is_pinned_bit_for_bit(days: int) -> None:
    assert digest(fit_days(days)) == PINNED[days]


@pytest.mark.parametrize("seed", range(120))
def test_the_fit_equals_the_exhaustive_search_exactly(seed: int) -> None:
    rows, normalization, cap = random_rows(seed, 60)

    assert numerics._fit(rows, normalization, cap) == exhaustive_fit(rows, normalization, cap)


def test_breakpoints_one_ulp_apart_and_pieces_capped_throughout_are_searched_exactly() -> None:
    rows, normalization, cap = random_rows(7, 30)
    power = cap / 1.1
    neighbour = math.nextafter(power, math.inf)
    rows[0] = replace(rows[0], pieces=((power, 0.25), (neighbour, 0.25)))
    rows[1] = replace(rows[1], pieces=((5 * cap, 0.5),))
    assert cap / power != cap / neighbour

    assert numerics._fit(rows, normalization, cap) == exhaustive_fit(rows, normalization, cap)


def test_a_fit_without_rows_equals_the_exhaustive_search() -> None:
    _, normalization, cap = random_rows(5, 1)

    assert numerics._fit([], normalization, cap) == exhaustive_fit([], normalization, cap)


def test_a_fit_beyond_floating_range_is_refused_as_before() -> None:
    rows, _, cap = random_rows(3, 30)

    with pytest.raises(ValueError, match="must be finite"):
        numerics._fit(rows, 1e-308, cap)
    with pytest.raises(ValueError, match="must be finite"):
        exhaustive_fit(rows, 1e-308, cap)


@pytest.mark.parametrize("days", [10, 40])
def test_doubling_the_input_at_most_two_and_a_half_times_the_fit_work(days: int) -> None:
    single, _ = executed_lines(lambda: fit_days(days))
    double, _ = executed_lines(lambda: fit_days(2 * days))

    assert double / single <= 2.5
