from collections.abc import Callable, Iterator
from datetime import timedelta

import pytest
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor

from pyracast.forecasts.models import CollectionJob, RequestAttempt
from pyracast.plants.models import PlantEvent
from tests.support.domain import NOW, System, make_system
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)

SLOT = NOW - timedelta(days=30)

INITIAL = {
    "forecasts": ("forecasts", "0001_initial"),
    "plants": ("plants", "0001_initial"),
    "incidents": ("incidents", "0001_initial"),
    "metering": ("metering", "0001_initial"),
    "notifications": ("notifications", "0001_initial"),
}
NEW_CHECKS = {
    "forecasts_collectionjob": "collectionjob_endpoint_chk",
    "forecasts_requestattempt": "requestattempt_purpose_chk",
    "plants_plantevent": "plantevent_action_chk",
}


@pytest.fixture
def leaf() -> Iterator[None]:
    targets = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        yield
    finally:
        MigrationExecutor(connection).migrate(targets)


def _migrate(*targets: tuple[str, str]) -> None:
    MigrationExecutor(connection).migrate(list(targets))


def _forward() -> None:
    _migrate(*MigrationExecutor(connection).loader.graph.leaf_nodes())


def _checks(table: str) -> set[str]:
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, table)
    return {name for name, details in constraints.items() if details["check"]}


def _job_and_attempt(system: System) -> tuple[CollectionJob, RequestAttempt]:
    job = CollectionJob.objects.create(
        binding=system.binding, scheduled_slot=SLOT, target_date=SLOT.date(), available_at=SLOT
    )
    attempt = RequestAttempt.objects.create(
        provider_account=system.account,
        binding=system.binding,
        utc_day=NOW.date(),
        created_at=NOW,
        purpose="validation",
    )
    return job, attempt


def _job(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return _job_and_attempt(make_system())[0]


def _attempt(monkeypatch: pytest.MonkeyPatch) -> models.Model:
    return _job_and_attempt(make_system())[1]


def _plant_event(monkeypatch: pytest.MonkeyPatch) -> PlantEvent:
    return PlantEvent.objects.filter(plant=make_plant(monkeypatch)[1]).earliest("pk")


@pytest.mark.usefixtures("leaf")
def test_vocabulary_migrations_keep_conforming_rows_and_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    job, attempt = _job_and_attempt(system)
    event = PlantEvent.objects.filter(plant=plant).earliest("pk")
    _migrate(*INITIAL.values())
    assert not any(name in _checks(table) for table, name in NEW_CHECKS.items())

    _forward()

    assert all(name in _checks(table) for table, name in NEW_CHECKS.items())
    assert CollectionJob.objects.get(pk=job.pk).endpoint == "forecasts"
    assert RequestAttempt.objects.get(pk=attempt.pk).purpose == "validation"
    assert PlantEvent.objects.get(pk=event.pk).action == event.action
    _migrate(*INITIAL.values())
    assert not any(name in _checks(table) for table, name in NEW_CHECKS.items())


@pytest.mark.usefixtures("leaf")
@pytest.mark.parametrize(
    ("row", "field", "value", "target"),
    [
        (_job, "endpoint", "legacy_endpoint", INITIAL["forecasts"]),
        (_attempt, "purpose", "operator", INITIAL["forecasts"]),
        (_plant_event, "action", "legacy_action", INITIAL["plants"]),
    ],
)
def test_vocabulary_migration_refuses_stored_values_outside_the_vocabulary(
    row: Callable[[pytest.MonkeyPatch], models.Model],
    field: str,
    value: str,
    target: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = row(monkeypatch)
    model, allowed = type(stored), getattr(stored, field)
    table = model._meta.db_table
    _migrate(target)
    model._default_manager.filter(pk=stored.pk).update(**{field: value})

    with pytest.raises(ValueError, match=rf"{table}\.{field} holds values outside") as refused:
        _forward()

    assert repr(value) in str(refused.value)
    assert NEW_CHECKS[table] not in _checks(table)
    assert model._default_manager.filter(pk=stored.pk, **{field: value}).exists()
    model._default_manager.filter(pk=stored.pk).update(**{field: allowed})
    _forward()
    assert NEW_CHECKS[table] in _checks(table)
