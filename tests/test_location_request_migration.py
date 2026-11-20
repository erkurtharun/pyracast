import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from pyracast.regions.models import RegionLocationChangeRequest
from tests.support.domain import make_system
from tests.support.migrations import checks, forward, migrate

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = ("regions", "0004_region_purge_started_at")


def test_every_location_request_is_kept_with_an_empty_decision_reason() -> None:
    system = make_system()
    migrate(PREVIOUS)
    try:
        apps = MigrationExecutor(connection).loader.project_state([PREVIOUS]).apps
        requests = apps.get_model("regions", "RegionLocationChangeRequest")._default_manager
        reviewed = {"reviewed_by_id": system.admin.pk, "reviewed_at": timezone.now()}
        for status, review in (("pending", {}), ("approved", reviewed), ("rejected", reviewed)):
            requests.create(
                region_id=system.region.pk,
                requested_by_id=system.member.pk,
                base_revision=1,
                latitude=41,
                longitude=30,
                timezone="UTC",
                reason=f"{status} before the decision note",
                status=status,
                **review,
            )
    finally:
        forward()

    assert sorted(
        RegionLocationChangeRequest.objects.values_list("status", "reason", "decision_reason")
    ) == [
        ("approved", "approved before the decision note", ""),
        ("pending", "pending before the decision note", ""),
        ("rejected", "rejected before the decision note", ""),
    ]
    assert {"regionlocationchangerequest_review_chk", "regionlocationchangerequest_status_chk"} <= (
        checks("regions_regionlocationchangerequest")
    )
