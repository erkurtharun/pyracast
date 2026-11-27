from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, models, transaction

from pyracast.accounts.models import Membership, Tenant
from pyracast.db import violated_constraint
from pyracast.forecasts.models import CollectionSchedule
from pyracast.notifications.models import (
    DeliveryRecipient,
    ReminderMemberRole,
    ReminderRecipient,
    ReminderRecipientTie,
    ReminderSchedule,
)
from tests.support.domain import NOW, make_system
from tests.support.notifications import configured, ready

pytestmark = pytest.mark.django_db(transaction=True)

ADDRESS = "member@example.com"
SLOT = NOW + timedelta(hours=1)
TIMES = [
    ("text", "10:00"),
    ("hour_out_of_range", ["24:00"]),
    ("single_digit_hour", ["9:00"]),
    ("seconds", ["10:00:00"]),
    ("trailing_newline", ["10:00\n"]),
    ("number", [600]),
]


def refusal(rows: models.QuerySet[Any], **changes: object) -> str | None:
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        rows.update(**changes)
    return violated_constraint(raised.value)


def cases(field: str, values: Sequence[tuple[str, object]]) -> list[Any]:
    return [pytest.param(field, value, id=f"{field}-{name}") for name, value in values]


@pytest.mark.parametrize(("field", "value"), cases("times", TIMES))
def test_the_database_refuses_reminder_times_of_another_shape(field: str, value: object) -> None:
    _, schedule = configured()
    rows = ReminderSchedule.objects.filter(pk=schedule.pk)

    assert refusal(rows, **{field: value}) == f"reminderschedule_{field}_chk"


@pytest.mark.parametrize(("field", "value"), cases("times", TIMES))
def test_the_database_refuses_collection_times_of_another_shape(field: str, value: object) -> None:
    system, schedule = configured()
    collection = CollectionSchedule.objects.create(
        region=system.region, times=["10:00"], effective_from=schedule.effective_from
    )

    assert refusal(CollectionSchedule.objects.filter(pk=collection.pk), **{field: value}) == (
        "collectionschedule_times_chk"
    )


def test_collection_times_are_stored_in_the_order_given() -> None:
    system = make_system(approved=True)
    CollectionSchedule.objects.create(
        region=system.region, times=["18:00", "06:00"], effective_from=NOW
    )

    assert CollectionSchedule.objects.get(region=system.region).times == ["18:00", "06:00"]


def duplicate_refusal(create: Callable[[], object]) -> str | None:
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        create()
    return violated_constraint(raised.value)


def test_one_address_key_is_stored_once_per_schedule_delivery_and_member_tie() -> None:
    delivery = ready()
    row = ReminderRecipient.objects.get(schedule=delivery.schedule)
    ReminderRecipientTie.objects.create(recipient=row, membership_id=7)

    refusals = [
        duplicate_refusal(
            lambda: ReminderRecipient.objects.create(
                schedule=delivery.schedule, address=row.address.upper(), key=row.key
            )
        ),
        duplicate_refusal(
            lambda: DeliveryRecipient.objects.create(
                delivery=delivery, address=row.address.upper(), key=row.key
            )
        ),
        duplicate_refusal(
            lambda: ReminderRecipientTie.objects.create(recipient=row, membership_id=7)
        ),
    ]

    assert refusals == [
        "reminderrecipient_schedule_key_uniq",
        "deliveryrecipient_delivery_key_uniq",
        "reminderrecipienttie_recipient_membership_uniq",
    ]


def test_empty_and_well_formed_times_are_stored() -> None:
    delivery = ready()
    schedules = ReminderSchedule.objects.filter(pk=delivery.schedule_id)

    schedules.update(times=[])
    schedules.update(times=["00:00", "23:59"])

    assert schedules.values_list("times", flat=True).get() == ["00:00", "23:59"]


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
