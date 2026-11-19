from collections.abc import Iterator
from datetime import timedelta

import pytest
from django.apps.registry import Apps
from django.contrib.auth.models import User
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor

from pyracast.accounts.models import Membership, WorkspaceRole
from pyracast.forecasts.models import CollectionSchedule
from tests.support.domain import NOW, System, make_system
from tests.support.migrations import checks, forward, migrate

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = (
    ("accounts", "0005_schema_naming_convention"),
    ("forecasts", "0004_schema_naming_convention"),
    ("notifications", "0005_schema_naming_convention"),
)


def historical(apps: Apps, label: str) -> models.Manager[models.Model]:
    app_label, model_name = label.split(".")
    model: type[models.Model] = apps.get_model(app_label, model_name)
    return model._default_manager


@pytest.fixture
def previous() -> Iterator[tuple[System, Apps]]:
    system = make_system(approved=True)
    migrate(*PREVIOUS)
    try:
        yield system, MigrationExecutor(connection).loader.project_state(PREVIOUS).apps
    finally:
        forward()


def test_every_effective_reminder_recipient_becomes_a_row(
    previous: tuple[System, Apps], capsys: pytest.CaptureFixture[str]
) -> None:
    system, apps = previous
    other = User.objects.create_user(username="other", email="other@example.com")
    member = Membership.objects.get(user=system.member).pk
    other_member = Membership.objects.create(
        tenant=system.tenant, user=other, role=WorkspaceRole.VIEWER
    ).pk
    schedule = historical(apps, "notifications.ReminderSchedule").create(
        region_id=system.region.pk,
        enabled=True,
        recipients=["Admin@Example.com", "ext@example.com", "gone@example.com", "both@example.com"],
        recipient_memberships={
            "admin@example.com": [member],
            "gone@example.com": [999_999],
            "both@example.com": [999_998, other_member, member],
        },
        times=["10:00"],
        effective_from=NOW,
        language="en",
    )
    deliveries = historical(apps, "notifications.NotificationDelivery")
    common = {
        "schedule_id": schedule.pk,
        "binding_id": system.binding.pk,
        "available_at": NOW,
        "preparation_deadline": NOW + timedelta(minutes=15),
    }
    sent = deliveries.create(
        scheduled_for=NOW,
        status="sent",
        recipients=["ext@example.com", "Admin@Example.com", "gone@example.com"],
        recipient_memberships={"admin@example.com": [member], "gone@example.com": [999_999]},
        revoked_recipients=["ext@example.com"],
        **common,
    )
    paused = deliveries.create(
        scheduled_for=NOW + timedelta(days=1),
        status="skipped",
        last_error="no_recipients",
        recipients=[],
        recipient_memberships={},
        **common,
    )

    forward()

    from pyracast.notifications.models import DeliveryRecipient, ReminderRecipient

    assert list(
        ReminderRecipient.objects.filter(schedule_id=schedule.pk)
        .order_by("pk")
        .values_list("email", "membership_id")
    ) == [
        ("Admin@Example.com", member),
        ("ext@example.com", None),
        ("both@example.com", other_member),
    ]
    assert list(
        DeliveryRecipient.objects.filter(delivery_id=sent.pk)
        .order_by("pk")
        .values_list("email", "membership_id", "revoked")
    ) == [
        ("ext@example.com", None, True),
        ("Admin@Example.com", member, False),
        ("gone@example.com", 999_999, False),
    ]
    assert not DeliveryRecipient.objects.filter(delivery_id=paused.pk).exists()
    assert "1 reminder recipient(s) pinned only to removed memberships" in capsys.readouterr().out


def test_collection_times_are_repaired_to_their_valid_local_times(
    previous: tuple[System, Apps], capsys: pytest.CaptureFixture[str]
) -> None:
    system, apps = previous
    stored = historical(apps, "forecasts.CollectionSchedule")
    stored.create(
        region_id=system.region.pk,
        times=["07:00", "7:00", 5, "07:00", "25:00", "06:30"],
        effective_from=NOW,
    )

    forward()

    assert CollectionSchedule.objects.get(region=system.region).times == ["06:30", "07:00"]
    assert "collectionschedule_times_chk" in checks("forecasts_collectionschedule")
    assert "1 collection schedule(s) held times outside HH:MM" in capsys.readouterr().out


def test_a_collection_schedule_without_a_time_list_is_emptied(
    previous: tuple[System, Apps],
) -> None:
    system, apps = previous
    stored = historical(apps, "forecasts.CollectionSchedule")
    stored.create(region_id=system.region.pk, times={"07:00": True}, effective_from=NOW)

    forward()

    assert CollectionSchedule.objects.get(region=system.region).times == []


def test_repeated_and_oversized_addresses_are_skipped_and_counted(
    previous: tuple[System, Apps], capsys: pytest.CaptureFixture[str]
) -> None:
    system, apps = previous
    oversized = "x" * 250 + "@example.com"
    schedule = historical(apps, "notifications.ReminderSchedule").create(
        region_id=system.region.pk,
        recipients=["ext@example.com", "EXT@example.com", oversized],
        recipient_memberships={},
        times=["10:00"],
        effective_from=NOW,
        language="en",
    )
    delivery = historical(apps, "notifications.NotificationDelivery").create(
        schedule_id=schedule.pk,
        binding_id=system.binding.pk,
        scheduled_for=NOW,
        available_at=NOW,
        preparation_deadline=NOW,
        status="sent",
        recipients=["ext@example.com", "Ext@example.com", oversized],
        recipient_memberships={},
        revoked_recipients=[],
    )

    forward()

    from pyracast.notifications.models import DeliveryRecipient, ReminderRecipient

    assert list(ReminderRecipient.objects.values_list("email", flat=True)) == ["ext@example.com"]
    assert list(
        DeliveryRecipient.objects.filter(delivery_id=delivery.pk).values_list("email", flat=True)
    ) == ["ext@example.com"]
    printed = capsys.readouterr().out
    assert "2 repeated recipient address(es) were kept once" in printed
    assert "2 recipient address(es) longer than 254 characters were not kept" in printed


def test_valid_collection_times_are_kept_as_stored(
    previous: tuple[System, Apps], capsys: pytest.CaptureFixture[str]
) -> None:
    system, apps = previous
    historical(apps, "forecasts.CollectionSchedule").create(
        region_id=system.region.pk, times=["18:00", "06:00"], effective_from=NOW
    )

    forward()

    assert CollectionSchedule.objects.get(region=system.region).times == ["18:00", "06:00"]
    assert "collection schedule" not in capsys.readouterr().out
