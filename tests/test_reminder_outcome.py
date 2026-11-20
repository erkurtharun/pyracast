from datetime import timedelta
from typing import Any

import pytest
from django.test import Client

from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import NotificationDelivery, NotificationSettings
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.scheduler import enqueue_due
from pyracast.regions.operations import submit_region
from tests.support.budgets import assert_scale_invariant
from tests.support.domain import NOW, System
from tests.support.notification_groups import member, second_workspace
from tests.support.notifications import SLOT, configured, saved

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def sites(system: System) -> dict[int, dict[str, Any]]:
    client = Client()
    client.force_login(system.member)
    response = client.get(f"/api/tenants/{system.tenant.pk}/sites/")
    assert response.status_code == 200, response.content
    return {site["id"]: site for site in response.json()["results"]}


def test_the_plant_list_names_each_plant_reminder_state_and_latest_outcome() -> None:
    system, schedule = configured()
    saved(system)
    NotificationDelivery.objects.create(
        schedule=schedule,
        binding=system.binding,
        scheduled_for=SLOT - timedelta(days=1),
        preparation_deadline=SLOT - timedelta(days=1),
        available_at=SLOT - timedelta(days=1),
        status="failed",
        last_error="mail_rejected",
    )
    (delivery_id,) = enqueue_due(SLOT)
    assert process_notification(delivery_id, SLOT)
    unconfigured = submit_region(
        system.member, system.tenant.pk, name="Bare", latitude=41, longitude=29, timezone="UTC"
    )
    other = second_workspace(system)
    configure_reminders(
        other.member,
        other.tenant.pk,
        other.region.pk,
        enabled=False,
        recipients=["other@example.com"],
        times=["10:00"],
        language="en",
        now=NOW,
    )

    listed = sites(system)

    assert set(listed) == {system.region.pk, unconfigured.pk}
    assert listed[system.region.pk]["reminder"] == {
        "state": "sending",
        "last_delivery": {
            "scheduled_for": SLOT.isoformat().replace("+00:00", "Z"),
            "status": "sent",
            "last_error": "",
        },
    }
    assert listed[unconfigured.pk]["reminder"] is None


def test_a_plant_whose_reminders_never_ran_names_its_state_without_an_outcome() -> None:
    system, _ = configured()
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["a@example.com", "b@example.com"],
        times=["10:00"],
        language="en",
        now=NOW,
    )
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 1})

    assert sites(system)[system.region.pk]["reminder"] == {
        "state": "over_recipient_limit",
        "last_delivery": None,
    }


def test_the_plant_list_reads_reminder_outcomes_in_a_fixed_number_of_statements() -> None:
    system, _ = configured()
    added = 0

    def seed(count: int) -> None:
        nonlocal added
        for _ in range(count):
            added += 1
            region = submit_region(
                system.member,
                system.tenant.pk,
                name=f"Site {added}",
                latitude=41 + added / 10,
                longitude=30,
                timezone="Europe/Istanbul",
            )
            holder = member(system, f"holder-{added}@example.com")
            configured_schedule = configure_reminders(
                system.member,
                system.tenant.pk,
                region.pk,
                enabled=False,
                recipients=[holder.email, f"external-{added}@example.com"],
                include_workspace_members=True,
                times=["10:00"],
                language="en",
                now=NOW,
            )
            for day in range(2):
                NotificationDelivery.objects.create(
                    schedule=configured_schedule,
                    binding=system.binding,
                    scheduled_for=SLOT - timedelta(days=day),
                    preparation_deadline=SLOT - timedelta(days=day),
                    available_at=SLOT - timedelta(days=day),
                    status="skipped",
                    last_error="no_recipients",
                )

    client = Client()
    client.force_login(system.member)
    url = f"/api/tenants/{system.tenant.pk}/sites/"
    assert_scale_invariant(seed, lambda: client.get(url), small=1, large=4)
