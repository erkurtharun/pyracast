from collections.abc import Iterator
from datetime import timedelta

import pytest
from django.apps.registry import Apps
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, models, transaction
from django.db.migrations.executor import MigrationExecutor

from pyracast.accounts.models import Membership, Tenant
from pyracast.notifications.models import (
    DeliveryRecipient,
    ReminderMemberRole,
    ReminderRecipient,
    ReminderSchedule,
)
from tests.support.domain import NOW, System, make_system
from tests.support.migrations import constraints, forward, migrate

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = (("notifications", "0010_delivery_preparation_metadata"),)
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


def legacy_rows(system: System, apps: Apps) -> tuple[int, int]:
    schedule = manager(apps, "ReminderSchedule").create(
        region_id=system.region.pk, effective_from=NOW, times=["10:00"], enabled=True
    )
    manager(apps, "ReminderRecipient").create(
        schedule_id=schedule.pk, address="Ops@example.com", key="ops@example.com"
    )
    delivery = manager(apps, "NotificationDelivery").create(
        schedule_id=schedule.pk,
        binding_id=system.binding.pk,
        scheduled_for=SLOT,
        available_at=SLOT,
        preparation_deadline=SLOT,
        status="sent",
        transmit_started_at=SLOT,
        sent_at=SLOT,
    )
    manager(apps, "DeliveryRecipient").create(
        delivery_id=delivery.pk, address="Ops@example.com", key="ops@example.com"
    )
    return int(schedule.pk), int(delivery.pk)


def columns(table: str) -> set[str]:
    with connection.cursor() as cursor:
        return {
            column.name for column in connection.introspection.get_table_description(cursor, table)
        }


def tables() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def functions(name: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_proc WHERE proname = %s", [name])
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def test_existing_schedules_stay_hidden_and_their_rows_carry_no_visible_role(
    previous: tuple[System, Apps],
) -> None:
    system, apps = previous
    schedule_id, delivery_id = legacy_rows(system, apps)

    forward()

    assert ReminderSchedule.objects.get(pk=schedule_id).hide_recipients is True
    assert list(
        ReminderRecipient.objects.filter(schedule_id=schedule_id).values_list(
            "address", "role", "role_members"
        )
    ) == [("Ops@example.com", "", [])]
    assert list(
        DeliveryRecipient.objects.filter(delivery_id=delivery_id).values_list("address", "role")
    ) == [("Ops@example.com", "bcc")]
    assert not ReminderMemberRole.objects.exists()
    assert {
        "reminderrecipient_role_chk",
        "reminderrecipient_role_members_chk",
    } <= constraints("notifications_reminderrecipient").keys()
    assert {"deliveryrecipient_role_chk"} <= constraints("notifications_deliveryrecipient").keys()
    assert {
        "remindermemberrole_schedule_membership_uniq",
        "remindermemberrole_role_chk",
    } <= constraints("notifications_remindermemberrole").keys()


def test_the_migration_reverses_to_the_previous_schema_and_keeps_every_row(
    previous: tuple[System, Apps],
) -> None:
    system, apps = previous
    schedule_id, delivery_id = legacy_rows(system, apps)
    forward()
    ReminderSchedule.objects.filter(pk=schedule_id).update(hide_recipients=False)
    ReminderRecipient.objects.filter(schedule_id=schedule_id).update(role="to")
    ReminderMemberRole.objects.create(
        schedule_id=schedule_id,
        membership=Membership.objects.get(user=system.member),
        role="cc",
    )

    migrate(*PREVIOUS)

    assert "hide_recipients" not in columns("notifications_reminderschedule")
    assert "role" not in columns("notifications_reminderrecipient")
    assert "role" not in columns("notifications_deliveryrecipient")
    assert "role_members" not in columns("notifications_reminderrecipient")
    assert "notifications_remindermemberrole" not in tables()
    assert functions("guard_remindermemberrole_workspace") == 0
    assert manager(apps, "ReminderRecipient").filter(schedule_id=schedule_id).count() == 1
    assert manager(apps, "DeliveryRecipient").filter(delivery_id=delivery_id).count() == 1


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (ReminderRecipient, {"address": "a@example.com", "key": "a@example.com", "role": "all"}),
        (DeliveryRecipient, {"address": "a@example.com", "key": "a@example.com", "role": ""}),
        (ReminderMemberRole, {"role": ""}),
    ],
)
def test_a_role_outside_the_vocabulary_is_refused_by_the_database(
    model: type[models.Model], values: dict[str, str]
) -> None:
    system = make_system(approved=True)
    schedule = ReminderSchedule.objects.create(region=system.region, effective_from=NOW)
    owner: dict[str, object] = {"schedule": schedule}
    if model is DeliveryRecipient:
        owner = {
            "delivery": schedule.notificationdelivery_set.create(
                binding=system.binding,
                scheduled_for=SLOT,
                available_at=SLOT,
                preparation_deadline=SLOT,
            )
        }
    if model is ReminderMemberRole:
        owner["membership"] = Membership.objects.get(user=system.member)
    with pytest.raises(IntegrityError), transaction.atomic():
        model._default_manager.create(**owner, **values)


def test_one_member_has_one_choice_per_schedule() -> None:
    system = make_system(approved=True)
    schedule = ReminderSchedule.objects.create(region=system.region, effective_from=NOW)
    membership = Membership.objects.get(user=system.member)
    ReminderMemberRole.objects.create(schedule=schedule, membership=membership, role="to")
    with pytest.raises(IntegrityError), transaction.atomic():
        ReminderMemberRole.objects.create(schedule=schedule, membership=membership, role="cc")


def test_a_choice_for_a_member_of_another_workspace_is_refused_by_the_database() -> None:
    system = make_system(approved=True)
    schedule = ReminderSchedule.objects.create(region=system.region, effective_from=NOW)
    outsider = User.objects.create(username="outsider")
    other = Membership.objects.create(tenant=Tenant.objects.create(name="Other"), user=outsider)
    own = ReminderMemberRole.objects.create(
        schedule=schedule, membership=Membership.objects.get(user=system.member), role="to"
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        ReminderMemberRole.objects.create(schedule=schedule, membership=other, role="cc")
    with pytest.raises(IntegrityError), transaction.atomic():
        ReminderMemberRole.objects.filter(pk=own.pk).update(membership=other)

    assert list(ReminderMemberRole.objects.values_list("membership_id", flat=True)) == [
        own.membership_id
    ]


def typed_row() -> int:
    system = make_system(approved=True)
    schedule = ReminderSchedule.objects.create(region=system.region, effective_from=NOW)
    return int(
        ReminderRecipient.objects.create(
            schedule=schedule, address="a@example.com", key="a@example.com"
        ).pk
    )


def written(pk: int, role: str, members: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE notifications_reminderrecipient SET role = %s, role_members = %s::bigint[] "
            "WHERE id = %s",
            [role, members, pk],
        )


@pytest.mark.parametrize(
    ("role", "members"),
    [
        ("to", "{{1,2},{3,4}}"),
        ("to", "{1,NULL}"),
        ("cc", "{NULL}"),
        ("to", "{0}"),
        ("cc", "{3,-1}"),
        ("", "{5}"),
        ("bcc", "{5}"),
    ],
)
def test_members_of_a_typed_choice_in_another_shape_are_refused_by_the_database(
    role: str, members: str
) -> None:
    pk = typed_row()

    with pytest.raises(IntegrityError), transaction.atomic():
        written(pk, role, members)


@pytest.mark.parametrize(("role", "members", "stored"), [("", "{}", []), ("to", "{5}", [5])])
def test_members_of_a_typed_choice_in_its_shape_are_stored(
    role: str, members: str, stored: list[int]
) -> None:
    pk = typed_row()

    written(pk, role, members)

    assert ReminderRecipient.objects.values_list("role", "role_members").get(pk=pk) == (
        role,
        stored,
    )
