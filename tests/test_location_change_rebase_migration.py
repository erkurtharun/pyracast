import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from pyracast.regions.models import Region, RegionLocationChangeRequest
from pyracast.regions.operations import review_location_change
from tests.test_domain import NOW, make_system


@pytest.mark.django_db(transaction=True)
def test_upgrade_moves_pending_location_requests_to_the_region_revision() -> None:
    system = make_system(approved=True)
    executor = MigrationExecutor(connection)
    current = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("regions", "0008_remove_region_surface")])
        Region.objects.filter(pk=system.region.pk).update(revision=3)
        pending = RegionLocationChangeRequest.objects.create(
            region=system.region,
            requested_by=system.member,
            base_revision=1,
            latitude=41,
            longitude=30,
            timezone="UTC",
        )
        rejected = RegionLocationChangeRequest.objects.create(
            region=system.region,
            requested_by=system.member,
            base_revision=2,
            latitude=42,
            longitude=31,
            timezone="UTC",
            status="rejected",
            reviewed_by=system.admin,
            reviewed_at=NOW,
        )
        MigrationExecutor(connection).migrate(current)
        pending.refresh_from_db()
        rejected.refresh_from_db()
        assert (pending.base_revision, rejected.base_revision) == (3, 2)
        assert review_location_change(system.admin, pending.pk, approve=True).status == "approved"
    finally:
        MigrationExecutor(connection).migrate(current)
