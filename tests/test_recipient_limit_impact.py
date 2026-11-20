import pytest
from django.urls import reverse

from pyracast.notifications.models import NotificationSettings
from pyracast.notifications.operations import configure_reminders
from pyracast.plants.models import Plant
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
