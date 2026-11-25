import traceback
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pyracast.forecasts.faults import DamagedStoredDataError, decoding
from pyracast.incidents.evidence import _capacity, _moment, _recorded
from pyracast.plants.forecasting import _issued, _source_row
from pyracast.plants.plant_days import _figure


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (True, TypeError),
        ("5", TypeError),
        ([5], TypeError),
        (float("inf"), ValueError),
        (10**400, OverflowError),
        (-1, ValueError),
    ],
    ids=["boolean", "text", "list", "infinite", "beyond-a-float", "negative"],
)
def test_a_stored_available_capacity_that_is_not_a_finite_nonnegative_number_is_refused(
    value: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        _capacity(value)


def test_a_stored_available_capacity_keeps_a_number_and_a_missing_value() -> None:
    assert (_capacity(None), _capacity(2), _capacity(1.5), _capacity(0)) == (None, 2, 1.5, 0)


@pytest.mark.parametrize(
    ("value", "error"),
    [(False, TypeError), (0, TypeError), ("", ValueError), ("2026-11-09T09:15:00", ValueError)],
    ids=["false", "number", "empty", "naive"],
)
def test_a_stored_instant_that_is_not_aware_text_is_refused(
    value: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        _moment(value)


def test_a_stored_incident_revision_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(TypeError):
        _recorded(cast(Any, SimpleNamespace(snapshot=[1])))


def test_a_source_interval_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(TypeError):
        _source_row(None)


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_a_stored_figure_that_is_not_finite_is_refused(value: float) -> None:
    with pytest.raises(ValueError, match=str(value)):
        _figure(value)


def test_a_damage_traceback_does_not_quote_the_stored_value() -> None:
    with pytest.raises(DamagedStoredDataError) as damage, decoding("incident_revision", 7):
        datetime.fromisoformat("09.11.2026 09:15")
    formatted = "".join(traceback.format_exception(damage.value))
    assert "incident_revision" in formatted
    assert "09.11.2026" not in formatted


def _issuance(payloads: Any, scenarios: Any) -> Any:
    return SimpleNamespace(pk=7, payloads=payloads, outage_scenarios=scenarios)


GOOD: dict[str, Any] = {"quality_flags": [], "intervals": [], "provenance": {}}


def test_a_stored_issuance_shaped_as_written_is_answered() -> None:
    scenario = {"status": "estimated", "result": GOOD}
    assert _issued(_issuance({"60": GOOD}, {"60": scenario}), "60") == (GOOD, scenario)


@pytest.mark.parametrize(
    ("payloads", "scenarios"),
    [
        ([], {}),
        ({"60": GOOD}, []),
        ({"60": []}, {}),
        ({"60": GOOD | {"quality_flags": "x"}}, {}),
        ({"60": GOOD | {"intervals": "x"}}, {}),
        ({"60": GOOD | {"intervals": [{"start": 1}]}}, {}),
        ({"60": GOOD | {"provenance": None}}, {}),
        ({"60": GOOD | {"energy_mwh": True}}, {}),
        ({"60": GOOD | {"daylight": []}}, {}),
        ({"60": GOOD | {"provenance": {"collected_at": "2026-11-09T09:15:00"}}}, {}),
        ({"60": GOOD}, {"60": []}),
        ({"60": GOOD}, {"60": {"status": "estimated", "result": []}}),
        ({"60": GOOD}, {"60": {"status": "guessed", "result": GOOD}}),
        ({"60": GOOD}, {"60": {"result": GOOD}}),
    ],
    ids=[
        "payloads-not-an-object",
        "scenarios-not-an-object",
        "result-not-an-object",
        "flags-not-a-list",
        "intervals-not-an-array",
        "interval-start-not-text",
        "provenance-not-an-object",
        "energy-boolean",
        "daylight-not-an-object",
        "collected-at-naive",
        "scenario-not-an-object",
        "scenario-result-not-an-object",
        "scenario-status-unknown",
        "scenario-without-status",
    ],
)
def test_a_stored_issuance_that_is_not_shaped_as_written_is_refused(
    payloads: Any, scenarios: Any
) -> None:
    with pytest.raises(DamagedStoredDataError):
        _issued(_issuance(payloads, scenarios), "60")
