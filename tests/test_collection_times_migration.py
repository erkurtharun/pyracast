from collections.abc import Iterator

import pytest
from django.apps.registry import Apps
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor

from pyracast.forecasts.models import CollectionSchedule
from tests.support.domain import NOW, System, make_system
from tests.support.migrations import checks, forward, migrate

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = (("forecasts", "0004_schema_naming_convention"),)


def historical_schedules(apps: Apps) -> models.Manager[models.Model]:
    model: type[models.Model] = apps.get_model("forecasts", "CollectionSchedule")
    return model._default_manager


@pytest.fixture
def previous() -> Iterator[tuple[System, Apps]]:
    system = make_system(approved=True)
    migrate(*PREVIOUS)
    try:
        yield system, MigrationExecutor(connection).loader.project_state(PREVIOUS).apps
    finally:
        forward()


def test_collection_times_are_repaired_to_their_valid_local_times(
    previous: tuple[System, Apps], capsys: pytest.CaptureFixture[str]
) -> None:
    system, apps = previous
    historical_schedules(apps).create(
        region_id=system.region.pk,
        times=["07:00", "7:00", 5, "07:00", "25:00", "06:30"],
        effective_from=NOW,
    )

    forward()

    assert CollectionSchedule.objects.get(region=system.region).times == ["06:30", "07:00"]
    assert "collectionschedule_times_chk" in checks("forecasts_collectionschedule")
    assert "1 collection schedule(s) held times outside HH:MM" in capsys.readouterr().out


def test_a_collection_schedule_without_a_time_list_is_emptied(
    previous: tuple[System, Apps],
) -> None:
    system, apps = previous
    historical_schedules(apps).create(
        region_id=system.region.pk, times={"07:00": True}, effective_from=NOW
    )

    forward()

    assert CollectionSchedule.objects.get(region=system.region).times == []


def test_valid_collection_times_are_kept_as_stored(
    previous: tuple[System, Apps], capsys: pytest.CaptureFixture[str]
) -> None:
    system, apps = previous
    historical_schedules(apps).create(
        region_id=system.region.pk, times=["18:00", "06:00"], effective_from=NOW
    )

    forward()

    assert CollectionSchedule.objects.get(region=system.region).times == ["18:00", "06:00"]
    assert "collection schedule" not in capsys.readouterr().out
