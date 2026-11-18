from datetime import timedelta
from typing import Any, cast

import pytest

from pyracast.forecasts.projections import saved_day_payload
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.preparation import prepare_delivery
from pyracast.notifications.scheduler import enqueue_due
from pyracast.plants.models import PlantConfiguration
from tests.support.domain import NOW
from tests.support.invariants import stored_again
from tests.support.notification_preparation import complete_saved
from tests.support.notification_report_data import report_delivery
from tests.support.notifications import SLOT, cache_saved_days, configured

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("eligible_configuration", [True, False])
def test_delivery_completeness_matches_the_electrical_total(
    monkeypatch: pytest.MonkeyPatch, eligible_configuration: bool
) -> None:
    _, plant, delivery = report_delivery(monkeypatch)
    if not eligible_configuration:
        assert plant.current_configuration_id is not None
        PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(
            created_at=SLOT + timedelta(seconds=1)
        )

    assert prepare_delivery(delivery, SLOT)

    assert delivery.prepared_message is not None
    assert delivery.prepared_message["tomorrow_complete"] is eligible_configuration
    context = cast(dict[str, Any], delivery.prepared_message["report_context"])
    normal = context["days"][1]["normal"]
    if eligible_configuration:
        assert normal["daylight"]["energy_mwh"] is not None
    else:
        assert normal is None
        assert "Unavailable" in str(delivery.prepared_message["body"])


def test_region_delivery_completeness_uses_delivered_daylight_coverage() -> None:
    system, _ = configured()
    snapshot = complete_saved(system)
    snapshot.normalized_intervals = [
        interval
        for index, interval in enumerate(snapshot.normalized_intervals)
        if index not in (0, 24)
    ]
    stored_again(snapshot)
    cache_saved_days(snapshot, NOW)
    delivery = NotificationDelivery.objects.get(pk=enqueue_due(SLOT)[0])

    assert prepare_delivery(delivery, SLOT)

    assert delivery.snapshot is not None
    result = saved_day_payload(delivery.snapshot, SLOT.date() + timedelta(days=1))
    assert result is not None
    assert result["complete"] is False
    assert cast(dict[str, Any], result["daylight"])["energy_kwh_m2"] is not None
    assert delivery.prepared_message is not None
    assert delivery.prepared_message["tomorrow_complete"] is True


def test_unavailable_delivery_has_no_complete_total() -> None:
    configured()
    delivery = NotificationDelivery.objects.get(pk=enqueue_due(SLOT)[0])

    assert prepare_delivery(delivery, SLOT)

    assert delivery.prepared_message is not None
    assert delivery.prepared_message["tomorrow_complete"] is False
