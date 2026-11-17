from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection

from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import ProductionObservation
from pyracast.plants.production import production_history
from tests.support.plant_operations import make_plant
from tests.support.production_changes import provider
from tests.support.provisional_measurements import DAY, acquisition

pytestmark = pytest.mark.django_db(transaction=True)


def test_production_keeps_manual_metadata_and_effective_value_from_the_same_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    acquisition((configuration, provider(system, plant)))
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    published = False

    def record_after_selection(
        execute: Callable[..., Any], sql: str, params: Any, many: bool, context: Any
    ) -> Any:
        nonlocal published
        result = execute(sql, params, many, context)
        if not published and 'FROM "plants_productionobservation"' in sql:
            published = True
            ProductionObservation.objects.create(
                plant=plant,
                configuration=configuration,
                starts_at=start,
                ends_at=start + timedelta(hours=1),
                energy_mwh=7,
                source="meter",
                recorded_by=system.member,
            )
        return result

    with connection.execute_wrapper(record_after_selection):
        result = production_history(plant, start_date=DAY, end_date=DAY)
    assert published
    first = result["intervals"][0]
    assert first["observation"] is None
    assert first["effective"]["energy_mwh"] == 26
    refreshed = production_history(plant, start_date=DAY, end_date=DAY)["intervals"][0]
    assert refreshed["observation"]["energy_mwh"] == 7
    assert refreshed["effective"]["energy_mwh"] == 7
