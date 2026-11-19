from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.apps.registry import Apps
from django.contrib.auth.models import User
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor

from pyracast.accounts.models import Membership, WorkspaceRole
from pyracast.forecasts.models import CollectionSchedule
from pyracast.notifications.models import NotificationDelivery
from tests.support.domain import NOW, System, make_system
from tests.support.migrations import checks, forward, migrate
from tests.support.notifications import SLOT, addresses

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
        ("both@example.com", member),
    ]
    assert list(
        DeliveryRecipient.objects.filter(delivery_id=sent.pk)
        .order_by("pk")
        .values_list("email", "membership_id", "revoked", "authorized")
    ) == [
        ("ext@example.com", None, True, False),
        ("Admin@Example.com", member, False, True),
        ("gone@example.com", 999_999, False, True),
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


def stored_schedule(apps: Apps, system: System, **values: Any) -> models.Model:
    return historical(apps, "notifications.ReminderSchedule").create(
        region_id=system.region.pk,
        times=["10:00"],
        effective_from=NOW,
        language="en",
        **values,
    )


def stored_delivery(
    apps: Apps, system: System, schedule: models.Model, **values: Any
) -> models.Model:
    return historical(apps, "notifications.NotificationDelivery").create(
        schedule_id=schedule.pk,
        binding_id=system.binding.pk,
        available_at=NOW,
        preparation_deadline=NOW,
        revoked_recipients=[],
        **values,
    )


def test_an_oversized_address_stays_an_invalid_recipient_and_a_repeat_is_kept_once(
    previous: tuple[System, Apps], capsys: pytest.CaptureFixture[str]
) -> None:
    system, apps = previous
    oversized = "x" * 250 + "@example.com"
    schedule = stored_schedule(
        apps,
        system,
        enabled=True,
        recipients=["ext@example.com", "EXT@example.com", oversized],
        recipient_memberships={},
    )
    delivery = stored_delivery(
        apps,
        system,
        schedule,
        scheduled_for=NOW,
        status="sent",
        recipients=["ext@example.com", "Ext@example.com", oversized],
        recipient_memberships={},
    )

    forward()

    from pyracast.notifications.models import DeliveryRecipient, ReminderSchedule
    from pyracast.notifications.scheduler import slot_recipients

    migrated = ReminderSchedule.objects.get(pk=schedule.pk)
    assert addresses(migrated) == ["ext@example.com", oversized]
    assert migrated.enabled
    assert slot_recipients(migrated, 5) == ([], "invalid_recipient")
    assert list(
        DeliveryRecipient.objects.filter(delivery_id=delivery.pk).values_list("email", flat=True)
    ) == ["ext@example.com", oversized]
    assert "2 repeated recipient address(es) were kept once" in capsys.readouterr().out


def test_an_address_longer_than_an_index_entry_keeps_its_history_and_holds_its_schedule(
    previous: tuple[System, Apps], capsys: pytest.CaptureFixture[str]
) -> None:
    system, apps = previous
    unindexable = "x" * 3000 + "@example.com"
    schedule = stored_schedule(
        apps,
        system,
        enabled=True,
        recipients=["ext@example.com", unindexable],
        recipient_memberships={},
    )
    delivery = stored_delivery(
        apps,
        system,
        schedule,
        scheduled_for=NOW,
        status="sent",
        recipients=[unindexable, "ext@example.com"],
        recipient_memberships={},
    )

    forward()

    from pyracast.notifications.models import ReminderSchedule
    from pyracast.notifications.scheduler import enqueue_due

    migrated = ReminderSchedule.objects.get(pk=schedule.pk)
    assert (addresses(migrated), migrated.enabled) == (["ext@example.com", unindexable], True)
    assert addresses(NotificationDelivery.objects.get(pk=delivery.pk)) == [
        unindexable,
        "ext@example.com",
    ]
    (held,) = NotificationDelivery.objects.filter(pk__in=enqueue_due(SLOT))
    assert (held.status, held.last_error, addresses(held)) == ("skipped", "invalid_recipient", [])
    assert "not kept" not in capsys.readouterr().out


def test_membership_ids_beyond_the_identifier_range_keep_the_address_refused(
    previous: tuple[System, Apps],
) -> None:
    system, apps = previous
    member = Membership.objects.get(user=system.member).pk
    schedule = stored_schedule(
        apps,
        system,
        enabled=True,
        recipients=["far@example.com", "member@example.com"],
        recipient_memberships={"far@example.com": [2**63], "member@example.com": [float(member)]},
    )
    delivery = stored_delivery(
        apps,
        system,
        schedule,
        scheduled_for=NOW,
        status="pending",
        recipients=["far@example.com", "member@example.com"],
        recipient_memberships={"far@example.com": [2**63], "member@example.com": [float(member)]},
    )

    forward()

    from pyracast.notifications.models import DeliveryRecipient, ReminderSchedule
    from pyracast.notifications.recipients import permitted_recipients

    assert addresses(ReminderSchedule.objects.get(pk=schedule.pk)) == ["member@example.com"]
    assert list(
        DeliveryRecipient.objects.filter(delivery_id=delivery.pk).values_list(
            "email", "membership_id", "revoked"
        )
    ) == [("far@example.com", None, True), ("member@example.com", member, False)]
    assert permitted_recipients(NotificationDelivery.objects.get(pk=delivery.pk)) == [
        "member@example.com"
    ]


def test_an_address_pinned_to_several_members_stays_eligible_through_any_active_one(
    previous: tuple[System, Apps],
) -> None:
    system, apps = previous
    member = Membership.objects.get(user=system.member).pk
    inactive = Membership.objects.create(
        tenant=system.tenant,
        user=User.objects.create_user(username="former", is_active=False),
        role=WorkspaceRole.VIEWER,
    ).pk
    shared = {
        "inactive-first@example.com": [inactive, member],
        "removed-first@example.com": [999_999, member],
    }
    schedule = stored_schedule(
        apps, system, enabled=True, recipients=list(shared), recipient_memberships=shared
    )
    delivery = stored_delivery(
        apps,
        system,
        schedule,
        scheduled_for=NOW,
        status="pending",
        recipients=list(shared),
        recipient_memberships=shared,
    )

    forward()

    from pyracast.notifications.models import ReminderSchedule
    from pyracast.notifications.recipients import effective_recipients, permitted_recipients

    assert effective_recipients(ReminderSchedule.objects.get(pk=schedule.pk)) == list(shared)
    assert permitted_recipients(NotificationDelivery.objects.get(pk=delivery.pk)) == list(shared)


def test_an_address_pinned_to_two_members_stays_a_recipient_until_both_ties_end(
    previous: tuple[System, Apps],
) -> None:
    system, apps = previous
    first, second = (
        User.objects.create_user(username=name, email=f"{name}@example.com")
        for name in ("first", "second")
    )
    ties = [
        Membership.objects.create(tenant=system.tenant, user=user, role=WorkspaceRole.VIEWER).pk
        for user in (first, second)
    ]
    pins = {"shared@example.com": ties}
    schedule = stored_schedule(
        apps, system, enabled=True, recipients=["shared@example.com"], recipient_memberships=pins
    )
    delivery = stored_delivery(
        apps,
        system,
        schedule,
        scheduled_for=NOW,
        status="pending",
        recipients=["shared@example.com"],
        recipient_memberships=pins,
    )

    forward()

    from pyracast.accounts.operations import remove_workspace_member
    from pyracast.notifications.models import ReminderSchedule
    from pyracast.notifications.recipients import effective_recipients, permitted_recipients

    def recipients() -> tuple[list[str], list[str]]:
        return (
            effective_recipients(ReminderSchedule.objects.get(pk=schedule.pk)),
            permitted_recipients(NotificationDelivery.objects.get(pk=delivery.pk)),
        )

    assert recipients() == (["shared@example.com"], ["shared@example.com"])
    User.objects.filter(pk=first.pk).update(is_active=False)
    assert recipients() == (["shared@example.com"], ["shared@example.com"])
    User.objects.filter(pk=first.pk).update(is_active=True)
    remove_workspace_member(system.member, system.tenant.pk, first.pk)
    assert recipients() == (["shared@example.com"], ["shared@example.com"])
    remove_workspace_member(system.member, system.tenant.pk, second.pk)
    assert recipients() == ([], [])


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
