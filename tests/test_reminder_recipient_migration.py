from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.apps.registry import Apps
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor

from pyracast.accounts.models import Membership
from pyracast.notifications.models import (
    DeliveryRecipient,
    NotificationDelivery,
    ReminderRecipient,
    ReminderSchedule,
)
from pyracast.notifications.recipients import delivery_addresses
from tests.support.domain import NOW, System, make_system
from tests.support.migrations import forward, migrate
from tests.support.notification_groups import member

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = (("notifications", "0006_outbound_mail"),)
SLOT = NOW + timedelta(hours=1)


@pytest.fixture
def previous() -> Iterator[tuple[System, Apps]]:
    system = make_system(approved=True)
    migrate(*PREVIOUS)
    try:
        yield system, MigrationExecutor(connection).loader.project_state(PREVIOUS).apps
    finally:
        forward()


def manager(apps: Apps, name: str) -> models.Manager[models.Model]:
    model: type[models.Model] = apps.get_model("notifications", name)
    return model._default_manager


def old_schedule(system: System, apps: Apps, **values: Any) -> int:
    row = manager(apps, "ReminderSchedule").create(
        region_id=system.region.pk, effective_from=NOW, times=["10:00"], enabled=True, **values
    )
    return int(row.pk)


def old_delivery(system: System, apps: Apps, schedule_id: int, minute: int, **values: Any) -> int:
    slot = SLOT + timedelta(minutes=minute)
    row = manager(apps, "NotificationDelivery").create(
        schedule_id=schedule_id,
        binding_id=system.binding.pk,
        scheduled_for=slot,
        available_at=slot,
        preparation_deadline=slot,
        **values,
    )
    return int(row.pk)


def stored(schedule_id: int) -> list[tuple[str, list[int]]]:
    return [
        (row.address, sorted(tie.membership_id for tie in row.ties.all()))
        for row in ReminderRecipient.objects.filter(schedule_id=schedule_id).order_by("pk")
    ]


def rows(delivery_id: int) -> list[tuple[str, bool]]:
    return list(
        DeliveryRecipient.objects.filter(delivery_id=delivery_id)
        .order_by("pk")
        .values_list("address", "revoked")
    )


def test_every_schedule_address_and_pin_is_copied_as_stored(
    previous: tuple[System, Apps],
) -> None:
    system, apps = previous
    holder = member(system, "holder@example.com")
    held = Membership.objects.get(user=holder).pk
    schedule_id = old_schedule(
        system,
        apps,
        recipients=["Holder@Example.com", "gone@example.com", "external@example.com"],
        recipient_memberships={"holder@example.com": [held], "gone@example.com": [987654321]},
        include_workspace_members=True,
    )

    forward()

    assert stored(schedule_id) == [
        ("Holder@Example.com", [held]),
        ("gone@example.com", [987654321]),
        ("external@example.com", []),
    ]
    schedule = ReminderSchedule.objects.get(pk=schedule_id)
    assert (schedule.revision, schedule.include_workspace_members) == (0, True)


def test_a_delivery_marked_for_transmission_keeps_its_whole_list_as_its_envelope(
    previous: tuple[System, Apps],
) -> None:
    system, apps = previous
    schedule_id = old_schedule(system, apps, recipients=["a@example.com"])
    marked = {
        status: old_delivery(
            system,
            apps,
            schedule_id,
            minute,
            status=status,
            recipients=["a@example.com", "b@example.com"],
            revoked_recipients=["b@example.com"],
            transmit_started_at=SLOT,
            lease_token=None if status != "uncertain" else "00000000-0000-0000-0000-000000000001",
            lease_expires_at=None if status != "uncertain" else SLOT,
            prepared_message={"format": "plain", "kind": "unavailable"},
        )
        for minute, status in enumerate(("sent", "failed", "uncertain", "retry"))
    }

    forward()

    for delivery_id in marked.values():
        assert rows(delivery_id) == [("a@example.com", False), ("b@example.com", True)]
        delivery = NotificationDelivery.objects.get(pk=delivery_id)
        assert delivery_addresses(delivery) == ["a@example.com", "b@example.com"]


@pytest.mark.parametrize(
    "status", ["pending", "retry", "sending", "uncertain", "skipped", "cancelled"]
)
def test_a_delivery_without_a_marker_lists_its_addresses_not_revoked_whatever_its_status(
    previous: tuple[System, Apps], status: str
) -> None:
    system, apps = previous
    schedule_id = old_schedule(system, apps, recipients=["a@example.com"])
    leased = status in {"sending", "uncertain"}
    delivery_id = old_delivery(
        system,
        apps,
        schedule_id,
        0,
        status=status,
        recipients=["a@example.com", "B@example.com"],
        recipient_memberships={"a@example.com": [7]},
        revoked_recipients=["b@example.com"],
        lease_token="00000000-0000-0000-0000-000000000002" if leased else None,
        lease_expires_at=SLOT if leased else None,
        prepared_message={"format": "plain", "kind": "unavailable"} if leased else None,
    )

    forward()

    assert rows(delivery_id) == [("a@example.com", False), ("B@example.com", True)]
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery_addresses(delivery) == ["a@example.com"]


def test_two_spellings_of_one_address_are_kept_once_with_every_pin(
    previous: tuple[System, Apps], capsys: pytest.CaptureFixture[str]
) -> None:
    system, apps = previous
    schedule_id = old_schedule(
        system,
        apps,
        recipients=["ops@example.com", " OPS@example.com"],
        recipient_memberships={"ops@example.com": [3], " ops@example.com": [4]},
    )
    delivery_id = old_delivery(
        system,
        apps,
        schedule_id,
        0,
        recipients=["ops@example.com", "Ops@example.com "],
        revoked_recipients=["ops@example.com "],
    )

    forward()

    assert stored(schedule_id) == [("ops@example.com", [3, 4])]
    assert rows(delivery_id) == [("ops@example.com", True)]
    assert "2 reminder address(es) repeated another spelling" in capsys.readouterr().out
