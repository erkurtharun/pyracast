from datetime import date, timedelta

from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import Plant
from tests.support.domain import NOW, System
from tests.support.performance_history import activated_calibration, estimated_actuals
from tests.support.plant_operations import publish_snapshot, snapshot_for

TODAY = date(2026, 11, 9)
DAY = date(2026, 11, 8)
UNCOVERED = date(2026, 11, 7)


def usable_calibration(system: System, plant: Plant) -> None:
    configuration = plant.current_configuration
    assert configuration is not None
    activated_calibration(system, configuration, 0.9, date(2026, 10, 20), NOW - timedelta(days=12))


def add_history(system: System, plant: Plant, copies: range) -> None:
    """Older acquisitions of every source behind the reads of today, tomorrow, yesterday and an
    earlier day without estimated actuals, and newer activations whose outage history changed;
    the copy with the lowest number is the one every read selects."""
    configuration = plant.current_configuration
    assert configuration is not None
    for copy in copies:
        for day in (UNCOVERED, DAY, TODAY, TODAY + timedelta(days=1)):
            start, _ = local_day_bounds(day, system.binding.timezone)
            publish_snapshot(
                snapshot_for(
                    system,
                    day,
                    power_kw=0.4 + copy / 100,
                    collected_at=min(start, NOW) - timedelta(hours=3, minutes=copy),
                )
            )
        _, end = local_day_bounds(DAY, system.binding.timezone)
        estimated_actuals(system, DAY, 0.3 + copy / 100, end + timedelta(hours=1, minutes=-copy))
        estimated_actuals(system, TODAY, 0.35, NOW - timedelta(minutes=30 + copy), hours=(0, 10))
        activated_calibration(
            system,
            configuration,
            0.7,
            date(2026, 10, 25),
            NOW - timedelta(days=2, minutes=copy),
            incident_revision_ids=[999_999],
        )
