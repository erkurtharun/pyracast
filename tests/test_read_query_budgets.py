from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from pyracast.accounts.models import Tenant
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_archive import select_forecast_issuances
from pyracast.plants.models import Plant, PlantConfiguration, ProductionObservation
from pyracast.plants.performance import forecast_performance
from pyracast.plants.production import production_history
from pyracast.regions.models import Region
from tests.test_forecast_performance import publish, selected_factor
from tests.test_plant_operations import make_plant, snapshot_for
from tests.test_production_changes import provider
from tests.test_provisional_measurements import DAY, acquisition

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("cutoff", [None, time(16)])
def test_archived_performance_uses_one_range_selection(
    monkeypatch: pytest.MonkeyPatch, cutoff: time | None
) -> None:
    _, plant = make_plant(monkeypatch)
    with CaptureQueriesContext(connection) as queries:
        result = forecast_performance(
            plant,
            start_date=date(2026, 8, 11),
            end_date=date(2026, 11, 8),
            issuance_cutoff=cutoff,
        )
    assert result["requested_days"] == result["missing_days"] == 90
    assert len(queries) <= 5


@pytest.mark.parametrize(
    ("zone", "first"),
    [
        ("Europe/Istanbul", date(2026, 11, 6)),
        ("Europe/London", date(2026, 3, 28)),
        ("Europe/London", date(2026, 10, 24)),
    ],
)
def test_range_selection_retains_each_days_cutoff_and_acquisition_tiebreak(
    monkeypatch: pytest.MonkeyPatch, zone: str, first: date
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    system.binding.timezone = zone
    system.binding.save(update_fields=("timezone",))
    PlantConfiguration.objects.filter(pk=configuration.pk).update(
        created_at=datetime.combine(first - timedelta(days=1), time(), ZoneInfo(zone))
        - timedelta(days=1)
    )
    expected_ids = []
    cutoffs = []
    last = first + timedelta(days=2)
    for offset in range(3):
        day = first + timedelta(days=offset)
        cutoff = datetime.combine(
            day - timedelta(days=1), time(16), ZoneInfo(system.binding.timezone)
        )
        cutoffs.append(cutoff)
        for power in (0.4, 0.5):
            selected = snapshot_for(system, day, power_kw=power, collected_at=cutoff)
            publish(selected)
        expected_ids.append(selected.pk)
        late_publication = snapshot_for(system, day, collected_at=cutoff - timedelta(minutes=1))
        publish(late_publication, published_at=cutoff + timedelta(seconds=1))
        late_issue = snapshot_for(
            system, day, collected_at=cutoff, issued_at=cutoff + timedelta(seconds=1)
        )
        publish(late_issue)
        publish(snapshot_for(system, day, collected_at=cutoff + timedelta(seconds=1)))
        start, end = local_day_bounds(day, system.binding.timezone)
        while start < end:
            ProductionObservation.objects.create(
                plant=plant,
                configuration=configuration,
                starts_at=start,
                ends_at=start + timedelta(hours=1),
                energy_mwh=65,
                source="meter",
                recorded_by=system.member,
            )
            start += timedelta(hours=1)
    with CaptureQueriesContext(connection) as queries:
        result = forecast_performance(
            plant, start_date=first, end_date=last, issuance_cutoff=time(16)
        )
    assert result["matched_days"] == 3
    assert [row["snapshot_id"] for row in result["days"]] == expected_ids
    assert [row["selection_cutoff"] for row in result["days"]] == cutoffs
    assert [row["predicted_energy_mwh"] for row in result["days"]] == [
        (
            local_day_bounds(first + timedelta(days=offset), zone)[1]
            - local_day_bounds(first + timedelta(days=offset), zone)[0]
        ).total_seconds()
        / 3600
        * 65
        for offset in range(3)
    ]
    assert result["metrics"]["daily_mae_mwh"] == 0
    assert len(queries) <= 5


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
    assert first["provider_original"]["energy_mwh"] == 26
    assert first["provider_original"]["snapshot_id"] == source.pk
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
    assert all(row["provider_original"]["energy_mwh"] == 26 for row in result["intervals"])
    assert all(
        row["provider_original"]["snapshot_id"] == original.pk for row in result["intervals"]
    )


def test_range_selector_keeps_mixed_cutoffs_missing_days_and_plant_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    days = [date(2026, 11, 7), date(2026, 11, 8)]
    expected = {}
    cutoffs: dict[date, datetime | None] = {}
    for day in days:
        start, _ = local_day_bounds(day, system.binding.timezone)
        cutoff = start - timedelta(hours=4)
        early = snapshot_for(system, day, collected_at=cutoff)
        late = snapshot_for(system, day, collected_at=cutoff + timedelta(hours=1))
        publish(early)
        publish(late)
        cutoffs[day] = None if day == days[0] else cutoff
        expected[day] = late.pk if day == days[0] else early.pk
    missing = days[0] - timedelta(days=1)
    cutoffs[missing] = None
    with CaptureQueriesContext(connection) as queries:
        selected = select_forecast_issuances(plant, cutoffs, native_only=True)
    assert len(queries) == 1
    assert {day: issuance.snapshot_id for day, issuance in selected.items()} == expected
    other = Plant.objects.create(
        name="Other",
        region=Region.objects.create(
            tenant=Tenant.objects.create(name="Other"),
            name="Other",
            latitude=41,
            longitude=30,
            timezone="Europe/Istanbul",
        ),
    )
    assert select_forecast_issuances(other, cutoffs) == {}
    with CaptureQueriesContext(connection) as queries:
        assert select_forecast_issuances(plant, {}) == {}
    assert len(queries) == 0


def test_native_performance_keeps_frozen_calibration_without_loading_its_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    day = date(2026, 11, 8)
    source = snapshot_for(system, day)
    selected_factor(system, configuration, source.collected_at)
    publish(source)
    with CaptureQueriesContext(connection) as queries:
        result = forecast_performance(plant, start_date=day, end_date=day)
    assert result["days"][0]["factor"] == 0.8
    assert result["days"][0]["calibration_id"] is not None
    assert result["days"][0]["snapshot_id"] == source.pk
    assert not any(
        f'"plants_calibrationrun"."{field}"' in query["sql"]
        for field in ("dataset_manifest", "result")
        for query in queries.captured_queries
    )
    assert len(queries) <= 5
