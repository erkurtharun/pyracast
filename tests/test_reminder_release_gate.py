from collections.abc import Iterator

import pytest
from allauth.account.models import EmailAddress
from django.core.exceptions import ValidationError
from django.test import Client

from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import (
    DeliveryRecipient,
    RecipientRole,
    ReminderRecipient,
    ReminderSchedule,
)
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.scheduler import enqueue_due
from tests.smtp_server import Script
from tests.support.domain import NOW, System, make_system
from tests.support.notification_groups import group, member
from tests.support.notifications import SLOT, chosen, parsed, smtp_wire

pytestmark = pytest.mark.django_db(transaction=True)

ADMIN = "admin@example.com"


@pytest.fixture(autouse=True)
def visible_reminder_mode_released() -> None:
    # Replaces the suite's fixture of this name, so the gate stays as shipped.
    return None


@pytest.fixture(name="smtp_wire")
def smtp_transactions() -> Iterator[Script]:
    with smtp_wire() as script:
        yield script


def administered() -> System:
    system = make_system(approved=True)
    system.member.email = ADMIN
    system.member.save(update_fields=["email"])
    EmailAddress.objects.create(user=system.member, email=ADMIN, verified=True, primary=True)
    return system


def stored_visible(system: System) -> ReminderSchedule:
    alice = member(system, "alice@example.com")
    schedule = group(system, ["boss@example.com"])
    ReminderSchedule.objects.filter(pk=schedule.pk).update(hide_recipients=False)
    ReminderRecipient.objects.filter(schedule=schedule).update(role=RecipientRole.TO)
    chosen(schedule, alice, RecipientRole.CC)
    schedule.refresh_from_db()
    return schedule


def reminders_url(system: System) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/"


def administrator_client(system: System) -> Client:
    client = Client()
    client.force_login(system.member)
    return client


def test_a_schedule_stored_visible_is_sent_with_every_address_in_blind_copy(
    smtp_wire: Script,
) -> None:
    system = administered()
    stored_visible(system)
    (delivery_id,) = enqueue_due(SLOT)

    assert process_notification(delivery_id, SLOT)

    (transaction,) = smtp_wire.transactions
    everyone = ["boss@example.com", ADMIN, "alice@example.com"]
    assert sorted(transaction.recipients) == sorted(everyone)
    message = parsed(transaction.data)
    assert (message["To"], message["Cc"], message["Bcc"]) == (None, None, None)
    assert all(address.encode() not in transaction.data for address in everyone)
    assert set(DeliveryRecipient.objects.values_list("role", flat=True)) == {RecipientRole.BCC}


def test_the_answer_of_a_schedule_stored_visible_is_hidden() -> None:
    system = administered()
    stored_visible(system)

    body = administrator_client(system).get(reminders_url(system)).json()

    assert body["hide_recipients"] is True


def test_until_the_release_a_visible_mode_is_refused_over_http_and_nothing_is_stored() -> None:
    system = administered()
    client = administrator_client(system)
    body = {
        "enabled": True,
        "include_workspace_members": True,
        "language": "en",
        "recipients": ["ops@example.com"],
        "times": ["17:00"],
        "expected_revision": 0,
    }
    url = reminders_url(system)

    refused = client.put(url, body | {"hide_recipients": False}, content_type="application/json")

    assert refused.status_code == 400
    assert refused.json()["code"] == "invalid_request"
    assert list(refused.json()["fields"]) == ["hide_recipients"]
    assert not ReminderSchedule.objects.exists()

    saved = client.put(url, body | {"hide_recipients": True}, content_type="application/json")

    assert saved.status_code == 200
    assert saved.json()["hide_recipients"] is True


def test_until_the_release_a_preview_of_the_visible_mode_answers_the_saves_refusal() -> None:
    system = administered()
    client = administrator_client(system)
    body = {
        "enabled": True,
        "include_workspace_members": True,
        "language": "en",
        "recipients": ["ops@example.com"],
        "times": ["17:00"],
        "expected_revision": 0,
        "hide_recipients": False,
    }
    url = reminders_url(system)

    previewed = client.post(f"{url}preview/", body, content_type="application/json")
    saved = client.put(url, body, content_type="application/json")

    assert previewed.status_code == 400
    assert (previewed.json()["code"], list(previewed.json()["fields"])) == (
        "invalid_request",
        ["hide_recipients"],
    )
    assert (saved.status_code, saved.json()) == (400, previewed.json())
    assert not ReminderSchedule.objects.exists()


def test_until_the_release_a_direct_save_of_the_visible_mode_is_refused_and_stores_nothing() -> (
    None
):
    system = administered()

    with pytest.raises(ValidationError) as refused:
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            enabled=True,
            recipients=["ops@example.com"],
            times=["17:00"],
            language="en",
            now=NOW,
            hide_recipients=False,
        )

    assert list(refused.value.message_dict) == ["hide_recipients"]
    assert not ReminderSchedule.objects.exists()
