from types import SimpleNamespace
from typing import Any, cast

import pytest

from pyracast.forecasts.faults import DamagedStoredDataError
from pyracast.incidents.evidence import _capacity, _recorded
from pyracast.plants.forecasting import _issued, _source_row
from pyracast.plants.plant_days import _figure


@pytest.mark.parametrize("value", [True, "5", [5]])
def test_a_stored_available_capacity_that_is_not_a_number_is_refused(value: object) -> None:
    with pytest.raises(TypeError):
        _capacity(value)


def test_a_stored_available_capacity_keeps_a_number_and_a_missing_value() -> None:
    assert (_capacity(None), _capacity(2), _capacity(1.5)) == (None, 2, 1.5)


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


def _issuance(payloads: Any, scenarios: Any) -> Any:
    return SimpleNamespace(pk=7, payloads=payloads, outage_scenarios=scenarios)


GOOD: dict[str, Any] = {"quality_flags": []}


@pytest.mark.parametrize(
    ("payloads", "scenarios"),
    [
        ([], {}),
        ({"60": GOOD}, []),
        ({"60": []}, {}),
        ({"60": {"quality_flags": "x"}}, {}),
        ({"60": GOOD}, {"60": []}),
        ({"60": GOOD}, {"60": {"result": []}}),
    ],
    ids=[
        "payloads-not-an-object",
        "scenarios-not-an-object",
        "result-not-an-object",
        "flags-not-a-list",
        "scenario-not-an-object",
        "scenario-result-not-an-object",
    ],
)
def test_a_stored_issuance_that_is_not_shaped_as_written_is_refused(
    payloads: Any, scenarios: Any
) -> None:
    with pytest.raises(DamagedStoredDataError):
        _issued(_issuance(payloads, scenarios), "60")
