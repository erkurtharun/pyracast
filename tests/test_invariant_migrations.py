import uuid
from collections.abc import Iterator
from datetime import date, timedelta

import pytest
from django.db.models import F

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    HistoricalReferenceImport,
    ReferenceCapacityNormalization,
)
from pyracast.metering.models import EpiasCollectionJob
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.plants.models import CalibrationRun
from tests.support.capacity_normalization import normalize, scaled_system
from tests.support.domain import NOW, System, make_system
from tests.support.invariants import (
    PROVIDER_ACCOUNT_RULE,
    SITE_BINDING_RULE,
    lease_violations,
    violations,
)
from tests.support.metering import connected
from tests.support.migrations import checks, constraints, forward, leaf_nodes, migrate, triggers
from tests.support.plant_operations import request_run
from tests.support.plant_training import actual_snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = {
    "connections": ("connections", "0002_initial"),
    "forecasts": ("forecasts", "0002_collection_endpoint_and_request_purpose_checks"),
    "metering": ("metering", "0002_epias_vocabulary_choices"),
    "notifications": ("notifications", "0002_delivery_vocabulary_checks"),
    "plants": ("plants", "0002_plant_event_action_and_calibration_error_checks"),
}
NEW_CHECKS = {
    "connections_provideraccount": {
        "provideraccount_daily_quota_chk",
        "provideraccount_credential_revision_chk",
        "provideraccount_authentication_failed_revision_chk",
    },
    "connections_sitebinding": {"sitebinding_validated_chk"},
    "forecasts_collectionjob": {
        "collectionjob_lease_chk",
        "collectionjob_reference_import_chk",
        "collectionjob_import_status_chk",
    },
    "forecasts_referencecapacitynormalization": {"referencecapacitynormalization_factor_chk"},
    "metering_epiascollectionjob": {"epiascollectionjob_lease_chk"},
    "plants_calibrationrun": {"calibrationrun_lease_chk"},
    "notifications_notificationdelivery": {"notificationdelivery_lease_chk"},
}
NEW_TRIGGERS = {
    "historicalreferenceimport_immutable",
    "epiassnapshot_immutable",
    "productionobservation_revision_guard",
}
CLEAN = dict.fromkeys(
    (
        "forecasts_collectionjob",
        "metering_epiascollectionjob",
        "plants_calibrationrun",
        "notifications_notificationdelivery",
    ),
    0,
)
UNAVAILABLE = {
    "format": "plain",
    "kind": "unavailable",
    "subject": "Pyracast forecast unavailable",
    "body": "Tomorrow's saved forecast could not be prepared.",
    "prepared_at": NOW.isoformat(),
}


@pytest.fixture
def leaf() -> Iterator[None]:
    targets = leaf_nodes()
    try:
        yield
    finally:
        migrate(*targets)


def _installed() -> tuple[set[str], set[str]]:
    found = {name for table in NEW_CHECKS for name in checks(table) if name in NEW_CHECKS[table]}
    unique = {
        name
        for name, details in constraints("connections_sitebinding").items()
        if details["unique"] and name == "sitebinding_region_region_revision_uniq"
    }
    return found | unique, triggers() & NEW_TRIGGERS


def _all_new() -> tuple[set[str], set[str]]:
    return (
        set().union(*NEW_CHECKS.values()) | {"sitebinding_region_region_revision_uniq"},
        NEW_TRIGGERS,
    )


@pytest.mark.usefixtures("leaf")
def test_invariant_migrations_repair_the_named_rows_and_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _ = connected(monkeypatch)
    collection = list(CollectionJob.objects.order_by("pk"))
    stray, unleased = (
        CollectionJob.objects.create(
            binding=system.binding,
            scheduled_slot=NOW + timedelta(hours=hours),
            target_date=NOW.date(),
            available_at=NOW,
        )
        for hours in (1, 2)
    )
    held = collection[0]
    epias = EpiasCollectionJob.objects.order_by("pk")[0]
    run = request_run(system, plant)
    delivery = _delivery(system)
    migrate(*PREVIOUS.values())
    assert _installed() == (set(), set())
    token = uuid.uuid4()
    CollectionJob.objects.filter(pk=stray.pk).update(lease_token=token, lease_expires_at=NOW)
    CollectionJob.objects.filter(pk=unleased.pk).update(status="running")
    CollectionJob.objects.filter(pk=held.pk).update(
        status="running", lease_token=token, lease_expires_at=NOW + timedelta(minutes=5)
    )
    EpiasCollectionJob.objects.filter(pk=epias.pk).update(status="running", lease_token=token)
    CalibrationRun.objects.filter(pk=run.pk).update(lease_expires_at=NOW)
    NotificationDelivery.objects.filter(pk=delivery.pk).update(
        status="uncertain", lease_token=token
    )
    ProviderAccount.objects.filter(pk=system.account.pk).update(
        daily_quota=12, credential_revision=0, authentication_failed_revision=3
    )
    SiteBinding.objects.filter(pk=system.binding.pk).update(validated_credential_revision=None)

    forward()

    assert _installed() == _all_new()
    assert lease_violations() == CLEAN
    stray.refresh_from_db()
    assert (stray.status, stray.lease_token, stray.lease_expires_at) == ("pending", None, None)
    unleased.refresh_from_db()
    assert unleased.status == "running"
    assert unleased.lease_token is not None
    assert unleased.lease_expires_at is not None
    held.refresh_from_db()
    assert (held.lease_token, held.lease_expires_at) == (token, NOW + timedelta(minutes=5))
    epias.refresh_from_db()
    assert (epias.status, epias.lease_token) == ("running", token)
    assert epias.lease_expires_at is not None
    run.refresh_from_db()
    assert (run.status, run.lease_token, run.lease_expires_at) == ("pending", None, None)
    delivery.refresh_from_db()
    assert (delivery.status, delivery.lease_token) == ("uncertain", token)
    assert delivery.lease_expires_at is not None
    account = ProviderAccount.objects.get(pk=system.account.pk)
    assert (
        account.daily_quota,
        account.credential_revision,
        account.authentication_failed_revision,
    ) == (10, 1, None)
    binding = SiteBinding.objects.get(pk=system.binding.pk)
    assert (binding.validated_at, binding.validated_credential_revision) == (None, None)
    assert violations("connections_provideraccount", PROVIDER_ACCOUNT_RULE) == 0
    assert violations("connections_sitebinding", SITE_BINDING_RULE) == 0

    migrate(*PREVIOUS.values())
    assert _installed() == (set(), set())


def _delivery(system: System) -> NotificationDelivery:
    return NotificationDelivery.objects.create(
        schedule=ReminderSchedule.objects.create(region=system.region, effective_from=NOW),
        scheduled_for=NOW,
        available_at=NOW,
        binding=system.binding,
        preparation_deadline=NOW,
        status="failed",
        last_error="mail_rejected",
        prepared_message=UNAVAILABLE,
    )


@pytest.mark.usefixtures("leaf")
def test_binding_migration_refuses_two_bindings_of_one_region_revision() -> None:
    system = make_system()
    duplicate = SiteBinding.objects.get(pk=system.binding.pk)
    duplicate.pk = None
    duplicate.region_revision += 1
    duplicate.save()
    migrate(PREVIOUS["connections"])
    SiteBinding.objects.filter(pk=duplicate.pk).update(
        region_revision=system.binding.region_revision
    )

    with pytest.raises(ValueError, match=r"connections_sitebinding holds 2 row.*one binding per"):
        forward()

    assert "sitebinding_region_region_revision_uniq" not in constraints("connections_sitebinding")
    assert SiteBinding.objects.filter(region=system.region).count() == 2
    SiteBinding.objects.filter(pk=duplicate.pk).update(region_revision=F("region_revision") + 1)
    forward()
    assert "sitebinding_region_region_revision_uniq" in constraints("connections_sitebinding")


@pytest.mark.usefixtures("leaf")
@pytest.mark.parametrize(
    ("values", "rule"),
    [
        ({"endpoint": "forecasts"}, "historical import endpoint"),
        ({"status": "pending"}, "succeeded import"),
    ],
)
def test_collection_migration_refuses_reference_imports_of_another_shape(
    values: dict[str, str], rule: str
) -> None:
    system = make_system()
    record = HistoricalReferenceImport.objects.create(
        binding=system.binding,
        sha256="a" * 64,
        source_name="fixture",
        source_settings={},
        imported_by=system.admin,
        imported_at=NOW,
        interval_count=1,
    )
    job = CollectionJob.objects.create(
        binding=system.binding,
        reference_import=record,
        endpoint="historical_import",
        scheduled_slot=NOW,
        target_date=NOW.date(),
        available_at=NOW,
        status="succeeded",
    )
    migrate(PREVIOUS["forecasts"])
    CollectionJob.objects.filter(pk=job.pk).update(**values)

    with pytest.raises(ValueError, match=rf"forecasts_collectionjob holds 1 row.*{rule}"):
        forward()

    assert not checks("forecasts_collectionjob") & NEW_CHECKS["forecasts_collectionjob"]
    assert CollectionJob.objects.filter(pk=job.pk, **values).exists()
    CollectionJob.objects.filter(pk=job.pk).update(endpoint="historical_import", status="succeeded")
    forward()
    assert NEW_CHECKS["forecasts_collectionjob"] <= checks("forecasts_collectionjob")


@pytest.mark.usefixtures("leaf")
def test_normalization_migration_refuses_a_factor_that_disagrees_with_its_capacities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    actual_snapshot_for(system, date(2026, 11, 7))
    normalize(system, target)
    migrate(PREVIOUS["forecasts"])
    ReferenceCapacityNormalization.objects.update(factor=F("factor") * 2)

    with pytest.raises(
        ValueError, match=r"forecasts_referencecapacitynormalization holds 1 row.*factor"
    ):
        forward()

    ReferenceCapacityNormalization.objects.update(factor=F("factor") / 2)
    forward()
    assert "referencecapacitynormalization_factor_chk" in checks(
        "forecasts_referencecapacitynormalization"
    )
