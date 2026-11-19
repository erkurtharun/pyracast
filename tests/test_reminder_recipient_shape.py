from collections.abc import Sequence
from typing import Any

import pytest
from django.db import IntegrityError, models, transaction

from pyracast.accounts.models import Membership
from pyracast.db import violated_constraint
from pyracast.forecasts.models import CollectionSchedule
from pyracast.notifications.models import (
    DeliveryRecipient,
    NotificationDelivery,
    ReminderRecipient,
    ReminderSchedule,
)
from tests.support.notifications import addresses, configured, ready

pytestmark = pytest.mark.django_db(transaction=True)

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


def cases(values: Sequence[tuple[str, object]]) -> list[Any]:
    return [pytest.param(value, id=name) for name, value in values]


@pytest.mark.parametrize("value", cases(TIMES))
def test_the_database_refuses_schedule_times_of_another_shape(value: object) -> None:
    system, schedule = configured()
    collection = CollectionSchedule.objects.create(
        region=system.region, times=["10:00"], effective_from=schedule.effective_from
    )

    assert refusal(ReminderSchedule.objects.filter(pk=schedule.pk), times=value) == (
        "reminderschedule_times_chk"
    )
    assert refusal(CollectionSchedule.objects.filter(pk=collection.pk), times=value) == (
        "collectionschedule_times_chk"
    )


@pytest.mark.parametrize("pinned", [True, False])
def test_an_address_is_stored_once_per_list_and_membership_whatever_its_case(
    pinned: bool,
) -> None:
    delivery = ready()
    membership = ReminderRecipient.objects.get(schedule=delivery.schedule).membership_id
    if not pinned:
        ReminderRecipient.objects.filter(schedule=delivery.schedule).update(membership=None)
        delivery.recipients.update(membership_id=None)
        membership = None

    with pytest.raises(IntegrityError) as schedule_row, transaction.atomic():
        ReminderRecipient.objects.create(
            schedule=delivery.schedule, email="DEFAULT@example.com", membership_id=membership
        )
    with pytest.raises(IntegrityError) as delivery_row, transaction.atomic():
        DeliveryRecipient.objects.create(
            delivery=delivery, email="Default@Example.com", membership_id=membership
        )

    assert violated_constraint(schedule_row.value) == "reminderrecipient_schedule_email_uniq"
    assert violated_constraint(delivery_row.value) == "deliveryrecipient_delivery_email_uniq"


def test_a_removed_membership_takes_its_saved_rows_and_leaves_the_delivery_history() -> None:
    delivery = ready()
    schedule = delivery.schedule
    ReminderRecipient.objects.create(schedule=schedule, email="external@example.com")
    pinned = ReminderRecipient.objects.get(schedule=schedule, email="default@example.com")
    assert pinned.membership_id is not None

    Membership.objects.filter(pk=pinned.membership_id).delete()

    assert addresses(schedule) == ["external@example.com"]
    assert list(
        NotificationDelivery.objects.get(pk=delivery.pk).recipients.values_list(
            "email", "membership_id"
        )
    ) == [("default@example.com", pinned.membership_id)]


def test_empty_and_well_formed_times_are_stored() -> None:
    _, schedule = configured()
    schedules = ReminderSchedule.objects.filter(pk=schedule.pk)

    schedules.update(times=[])
    schedules.update(times=["00:00", "23:59"])

    assert schedules.values_list("times", flat=True).get() == ["00:00", "23:59"]
