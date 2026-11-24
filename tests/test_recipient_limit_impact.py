import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.notifications.models import (
    NotificationSettings,
    ReminderRecipient,
    ReminderRecipientTie,
    ReminderSchedule,
)
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.recipients import effective_recipients, recipient_key
from pyracast.plants.models import Plant
from pyracast.platform_admin.recipient_limit import crowded_plants
from pyracast.regions.models import Region
from tests.support.api import verified_admin
from tests.support.domain import NOW, make_system

pytestmark = pytest.mark.django_db(transaction=True)

URL = "admin:workspace_settings"


def reminded_plant() -> None:
    system = make_system(approved=True)
    Plant.objects.create(region=system.region, name="Crowded park")
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=["a@example.com", "b@example.com", "c@example.com"],
        times=["10:00"],
        now=NOW,
    )


def limit() -> int:
    return NotificationSettings.objects.get().max_recipients


def test_lowering_the_limit_below_a_plant_s_list_asks_for_confirmation_naming_it() -> None:
    reminded_plant()
    client, _ = verified_admin()
    url = reverse(URL)
    before = client.get(url).content.decode()

    refused = client.post(url, {"max_recipients": 2, "expected_max_recipients": 5})
    confirmed = client.post(
        url, {"max_recipients": 2, "expected_max_recipients": 5, "confirm_pause": "on"}
    )
    after = client.get(url).content.decode()

    assert "No plant is over the limit." in before
    assert refused.status_code == 400
    page = refused.content.decode()
    assert "Crowded park" in page
    assert "Tenant" in page
    assert "pauses reminders" in page
    assert confirmed.status_code == 302
    assert limit() == 2
    assert "Crowded park" in after[after.index('id="over-limit-title"') :]


def test_a_change_that_pauses_no_new_plant_needs_no_confirmation() -> None:
    reminded_plant()
    client, _ = verified_admin()

    response = client.post(reverse(URL), {"max_recipients": 3, "expected_max_recipients": 5})

    assert response.status_code == 302
    assert limit() == 3


def crowded_workspace(index: int) -> ReminderSchedule:
    """A workspace whose plant reminds two verified members, one address tied to a member and
    one untied address."""
    tenant = Tenant.objects.create(name=f"Workspace {index}")
    region = Region.objects.create(
        tenant=tenant, name=f"Plant {index}", latitude=index, longitude=30, timezone="UTC"
    )
    schedule = ReminderSchedule.objects.create(
        region=region, enabled=True, include_workspace_members=True, effective_from=NOW
    )
    memberships = []
    for number in range(2):
        email = f"member{number}@w{index}.example.com"
        user = User.objects.create(username=f"member-{index}-{number}", email=email)
        EmailAddress.objects.create(user=user, email=email, primary=True, verified=True)
        memberships.append(
            Membership.objects.create(user=user, tenant=tenant, role=WorkspaceRole.VIEWER)
        )
    for address, tie in (
        (f"tied@w{index}.example.com", memberships[0]),
        (f"x@w{index}.example.com", None),
    ):
        row = ReminderRecipient.objects.create(
            schedule=schedule, address=address, key=recipient_key(address)
        )
        if tie is not None:
            ReminderRecipientTie.objects.create(recipient=row, membership_id=tie.pk)
    return schedule


def read_crowded(limit: int) -> tuple[int, list[int]]:
    with CaptureQueriesContext(connection) as queries:
        plants = crowded_plants(limit)
    return len(queries), [plant.recipients for plant in plants]


def test_plants_over_the_limit_are_read_in_a_fixed_number_of_queries() -> None:
    schedules = [crowded_workspace(1)]
    one_workspace = read_crowded(3)
    schedules += [crowded_workspace(index) for index in (2, 3)]
    three_workspaces = read_crowded(3)

    assert one_workspace == (4, [4])
    assert three_workspaces == (4, [4, 4, 4])
    assert [len(effective_recipients(schedule)) for schedule in schedules] == [4, 4, 4]
