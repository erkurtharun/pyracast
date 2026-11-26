from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import ProductionObservation
from pyracast.plants.production import production_history
from tests.support.plant_operations import make_plant
from tests.support.production_changes import provider
from tests.support.provisional_measurements import DAY, acquisition

pytestmark = pytest.mark.django_db(transaction=True)


def test_production_loads_provider_once_and_preserves_original_under_manual_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    source = acquisition((configuration, provider(system, plant)))
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    observation = ProductionObservation.objects.create(
        plant=plant,
        configuration=configuration,
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        energy_mwh=7,
        source="meter",
        recorded_by=system.member,
    )
    with CaptureQueriesContext(connection) as queries:
        result = production_history(plant, start_date=DAY, end_date=DAY)
    first = result["intervals"][0]
    assert first["effective"]["energy_mwh"] == 7
    assert first["provider_originals"][0]["energy_mwh"] == 26
    assert first["provider_originals"][0]["snapshot_id"] == source.pk
    assert first["observation"]["id"] == observation.pk
    provider_queries = [
        query["sql"]
        for query in queries.captured_queries
        if 'FROM "metering_epiassnapshot"' in query["sql"]
    ]
    assert len(provider_queries) == 1
    assert '"raw_payload"' not in provider_queries[0]


def test_production_freezes_effective_and_original_provider_values_for_one_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    source = configuration, provider(system, plant)
    original = acquisition(source)
    published = False

    def publish_after_selection(
        execute: Callable[..., Any], sql: str, params: Any, many: bool, context: Any
    ) -> Any:
        nonlocal published
        result = execute(sql, params, many, context)
        if not published and 'FROM "metering_epiassnapshot"' in sql:
            published = True
            acquisition(source, minute=1, values=["31"] * 24)
        return result

    with connection.execute_wrapper(publish_after_selection):
        result = production_history(plant, start_date=DAY, end_date=DAY)
    assert published
    assert all(row["effective"]["energy_mwh"] == 26 for row in result["intervals"])
    assert all(row["provider_originals"][0]["energy_mwh"] == 26 for row in result["intervals"])
    assert all(
        row["provider_originals"][0]["snapshot_id"] == original.pk for row in result["intervals"]
    )
