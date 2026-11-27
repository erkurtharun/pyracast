import pytest
from django.db import connection

from pyracast.plants.models import Plant
from tests.support.migrations import forward, migrate
from tests.support.plant_operations import make_plant
from tests.support.trash_operations import sibling_plant

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.usefixtures("leaf")
def test_migration_requests_preparation_for_every_configured_plant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, configured = make_plant(monkeypatch)
    unconfigured = sibling_plant(system)
    Plant.objects.filter(pk=unconfigured.pk).update(current_configuration=None)
    migrate(("plants", "0004_schema_naming_convention"))
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM plants_plant")
        assert cursor.fetchone() == (2,)

    forward()

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, calibration_preparation_due_at IS NOT NULL FROM plants_plant ORDER BY id"
        )
        assert cursor.fetchall() == [(configured.pk, True), (unconfigured.pk, False)]
