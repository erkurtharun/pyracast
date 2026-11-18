from collections.abc import Sequence
from typing import Any

import pytest
from django.db import IntegrityError, models, transaction

from pyracast.db import violated_constraint
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from tests.support.notifications import configured, ready

pytestmark = pytest.mark.django_db(transaction=True)

ADDRESS = "member@example.com"
TEXT_LISTS = [
    ("not_a_list", ADDRESS),
    ("object", {ADDRESS: 1}),
    ("number_item", [ADDRESS, 5]),
    ("null_item", [None]),
]
MEMBERSHIP_MAPS = [
    ("list", [ADDRESS]),
    ("scalar_members", {ADDRESS: 3}),
    ("text_member", {ADDRESS: ["3"]}),
    ("zero_member", {ADDRESS: [0]}),
    ("fractional_member", {ADDRESS: [1.5]}),
]
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *cases("recipients", TEXT_LISTS),
        *cases("recipient_memberships", MEMBERSHIP_MAPS),
        *cases("times", TIMES),
    ],
)
def test_the_database_refuses_a_reminder_schedule_of_another_shape(
    field: str, value: object
) -> None:
    _, schedule = configured()
    rows = ReminderSchedule.objects.filter(pk=schedule.pk)

    assert refusal(rows, **{field: value}) == f"reminderschedule_{field}_chk"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *cases("recipients", TEXT_LISTS),
        *cases("revoked_recipients", TEXT_LISTS),
        *cases("recipient_memberships", MEMBERSHIP_MAPS),
    ],
)
def test_the_database_refuses_a_delivery_of_another_shape(field: str, value: object) -> None:
    rows = NotificationDelivery.objects.filter(pk=ready().pk)

    assert refusal(rows, **{field: value}) == f"notificationdelivery_{field}_chk"


def test_empty_and_well_formed_recipients_and_times_are_stored() -> None:
    delivery = ready()
    schedules = ReminderSchedule.objects.filter(pk=delivery.schedule_id)
    deliveries = NotificationDelivery.objects.filter(pk=delivery.pk)
    memberships = {ADDRESS: [1, 2]}

    schedules.update(recipients=[], recipient_memberships={}, times=[])
    schedules.update(
        recipients=[ADDRESS], recipient_memberships=memberships, times=["00:00", "23:59"]
    )
    deliveries.update(recipients=[], recipient_memberships={}, revoked_recipients=[])
    deliveries.update(
        recipients=[ADDRESS], recipient_memberships=memberships, revoked_recipients=[ADDRESS]
    )

    assert schedules.values_list("times", flat=True).get() == ["00:00", "23:59"]
    assert deliveries.values_list("recipient_memberships", flat=True).get() == memberships
