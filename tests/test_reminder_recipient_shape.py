from collections.abc import Callable, Sequence
from typing import Any

import pytest
from django.db import IntegrityError, models, transaction

from pyracast.db import violated_constraint
from pyracast.forecasts.models import CollectionSchedule
from pyracast.notifications.models import (
    DeliveryRecipient,
    ReminderRecipient,
    ReminderRecipientTie,
    ReminderSchedule,
)
from tests.support.notifications import configured, ready

pytestmark = pytest.mark.django_db(transaction=True)

ADDRESS = "member@example.com"
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
