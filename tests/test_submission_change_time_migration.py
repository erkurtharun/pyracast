from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from pyracast.plants.models import PlantSubmission
from tests.support.admin_setup import submitted_plant
from tests.support.domain import make_system

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = ("plants", "0005_calibration_preparation_due_at")
CREATED = datetime(2026, 11, 1, 9, tzinfo=UTC)


@pytest.fixture
def leaf() -> Iterator[None]:
    targets = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        yield
    finally:
        MigrationExecutor(connection).migrate(targets)


@pytest.mark.usefixtures("leaf")
def test_every_stored_submission_keeps_its_row_and_was_last_changed_when_created() -> None:
    system = make_system()
    submission_id = PlantSubmission.objects.get(plant=submitted_plant(system)).pk
    PlantSubmission.objects.filter(pk=submission_id).update(created_at=CREATED)
    MigrationExecutor(connection).migrate([PREVIOUS])

    MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    submission = PlantSubmission.objects.get(pk=submission_id)
    assert (submission.created_at, submission.updated_at) == (CREATED, CREATED)
    assert PlantSubmission._meta.get_field("updated_at").null is False
