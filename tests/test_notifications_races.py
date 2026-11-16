from datetime import timedelta

import pytest
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import NotificationDelivery
from pyracast.trash import purge_expired, trash_region
from tests.test_domain import NOW
from tests.test_notifications import SLOT, ready
from tests.test_trash_boundaries import _race_before_region_lock

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("purge_region", [True, False])
def test_notification_candidate_deleted_before_authority_lock(
    monkeypatch: pytest.MonkeyPatch, purge_region: bool
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    delivery = ready()
    schedule = delivery.schedule
    assert schedule.configured_by is not None
    actor = schedule.configured_by

    def purge() -> None:
        if purge_region:
            trash_region(actor, schedule.region.tenant_id, schedule.region_id)
            purge_expired(NOW + timedelta(days=31))
        else:
            NotificationDelivery.objects.filter(pk=delivery.pk).delete()

    assert (
        _race_before_region_lock(
            monkeypatch,
            "first",
            lambda: process_notification(delivery.pk, SLOT),
            purge,
            lock_model=Tenant,
        )
        is False
    )
