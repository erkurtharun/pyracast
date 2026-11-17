import pytest
from django.utils import timezone

from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.scheduler import enqueue_due
from pyracast.plants.models import Plant
from pyracast.plants.operations import create_plant
from tests.support.domain import NOW, System
from tests.support.notification_preparation import complete_saved
from tests.support.notifications import SLOT, configured


def report_delivery(monkeypatch: pytest.MonkeyPatch) -> tuple[System, Plant, NotificationDelivery]:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system, _ = configured()
    plant = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        name="Electrical plant",
        dc_capacity_mw=130,
        export_limit_mw=100,
    )
    snapshot = complete_saved(system)
    delivery = NotificationDelivery.objects.get(pk=enqueue_due(SLOT)[0])
    delivery.snapshot = snapshot
    return system, plant, delivery
