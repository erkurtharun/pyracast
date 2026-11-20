from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from pyracast.regions.models import Region
from tests.support.domain import make_system
from tests.support.migrations import forward, migrate

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = ("regions", "0007_audit_region_resubmitted")


def test_every_region_is_kept_and_only_those_already_in_the_bin_are_never_warned() -> None:
    system = make_system()
    trashed_at = timezone.now() - timedelta(days=24)
    migrate(PREVIOUS)
    try:
        apps = MigrationExecutor(connection).loader.project_state([PREVIOUS]).apps
        regions = apps.get_model("regions", "Region")._default_manager
        for name, deleted_at in (("Live", None), ("Binned", trashed_at)):
            regions.create(
                tenant_id=system.tenant.pk,
                name=name,
                latitude=41 if deleted_at else 42,
                longitude=30,
                timezone="UTC",
                deleted_at=deleted_at,
            )
        before = sorted(regions.values_list("pk", "name", "status", "revision", "deleted_at"))
    finally:
        forward()

    rows = Region.objects.order_by("pk")
    assert sorted(rows.values_list("pk", "name", "status", "revision", "deleted_at")) == before
    marked = dict(rows.values_list("name", "purge_warning_sent_at"))
    assert marked["Live"] is None
    assert marked["Site"] is None
    assert marked["Binned"] is not None
