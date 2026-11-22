from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction

from pyracast.forecasts.models import ForecastSnapshot
from tests.support.domain import NOW, make_system
from tests.support.migrations import checks, forward, migrate
from tests.support.plant_operations import snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = ("forecasts", "0006_collection_outcomes_and_ledger_status")
TABLE = "forecasts_forecastsnapshot"


def columns() -> set[str]:
    with connection.cursor() as cursor:
        return {
            column.name for column in connection.introspection.get_table_description(cursor, TABLE)
        }


def test_snapshots_keep_their_acquisition_and_the_guard_once_weather_columns_go() -> None:
    system = make_system()
    staged = snapshot_for(system, NOW.date())
    published = snapshot_for(system, NOW.date() + timedelta(days=1), available_at=NOW)
    migrate(PREVIOUS)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE forecasts_forecastsnapshot SET weather_payload = %s, weather_error = %s "
                "WHERE id = %s",
                ('{"provider": "open_meteo"}', "weather_request_failed", staged.pk),
            )
    finally:
        forward()

    assert {"weather_payload", "weather_error"}.isdisjoint(columns())
    assert "forecastsnapshot_weather_error_chk" not in checks(TABLE)
    rows = ForecastSnapshot.objects.filter(pk=staged.pk)
    assert rows.values_list("raw_payload", flat=True).get() == staged.raw_payload
    with pytest.raises(IntegrityError, match="are immutable"), transaction.atomic():
        rows.update(raw_payload={"changed": True})
    rows.update(available_at=NOW)
    for snapshot in (staged, published):
        with pytest.raises(IntegrityError, match="are immutable"), transaction.atomic():
            ForecastSnapshot.objects.filter(pk=snapshot.pk).update(available_at=None)
