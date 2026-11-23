import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.db import close_old_connections, connection, connections
from django.db.models import F
from django.test import Client
from django.test.utils import CaptureQueriesContext

from pyracast.accounts.models import Membership, WorkspaceRole
from pyracast.connections.models import ProviderAccount
from pyracast.notifications.models import NotificationSettings, ReminderSchedule
from pyracast.notifications.operations import MAX_RECIPIENT_LIMIT, configure_reminders
from pyracast.notifications.overview import plants_at_recipient_limit
from pyracast.notifications.scheduler import enqueue_due
from pyracast.regions.models import Region
from pyracast.regions.operations import submit_region
from pyracast.trash import trash_region
from tests.support.domain import NOW, System, make_system
from tests.support.notifications import schedule_addresses, store

pytestmark = pytest.mark.django_db(transaction=True)


def verified(user: User, email: str) -> User:
    user.email = email
    user.save(update_fields=["email"])
    EmailAddress.objects.create(user=user, email=email, primary=True, verified=True)
    return user


def viewer_of(system: System) -> User:
    viewer = verified(User.objects.create_user(username="viewer"), "viewer@example.com")
    Membership.objects.create(tenant=system.tenant, user=viewer, role=WorkspaceRole.VIEWER)
    return viewer


def approved() -> System:
    system = make_system(approved=True)
    verified(system.member, "admin@example.com")
    return system


def url(system: System) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/"


def client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def submission(**changes: Any) -> dict[str, Any]:
    return {
        "enabled": True,
        "include_workspace_members": False,
        "language": "en",
        "recipients": ["ops@example.com"],
        "times": ["17:00"],
        "expected_revision": 0,
    } | changes


def put(system: System, **changes: Any) -> Any:
    revision = ReminderSchedule.objects.filter(region=system.region).values_list(
        "revision", flat=True
    )
    return client_for(system.member).put(
        url(system),
        submission(expected_revision=revision.first() or 0) | changes,
        content_type="application/json",
    )


def save(system: System, **changes: Any) -> ReminderSchedule:
    values = submission(**changes)
    del values["expected_revision"]
    return configure_reminders(system.member, system.tenant.pk, system.region.pk, now=NOW, **values)


def test_a_viewer_reads_recipient_counts_but_no_addresses() -> None:
    system = approved()
    viewer = viewer_of(system)
    save(system, include_workspace_members=True)
    enqueue_due(NOW + timedelta(hours=1))

    body = client_for(viewer).get(url(system)).json()

    assert "@" not in json.dumps(body)
    assert (body["recipient_count"], body["workspace_member_count"], body["addresses"]) == (
        3,
        2,
        None,
    )
    (delivery,) = body["deliveries"]
    assert (delivery["recipient_count"], delivery["recipients"]) == (3, None)


def test_the_administrator_reads_the_addresses() -> None:
    system = approved()
    viewer_of(system)
    save(system, include_workspace_members=True)
    enqueue_due(NOW + timedelta(hours=1))

    body = client_for(system.member).get(url(system)).json()

    memberships = dict(
        Membership.objects.filter(tenant=system.tenant).values_list("user__email", "pk")
    )
    assert body["addresses"] == {
        "recipients": ["ops@example.com"],
        "workspace_recipients": ["admin@example.com", "viewer@example.com"],
        "members": [
            {
                "membership": memberships["admin@example.com"],
                "address": "admin@example.com",
                "kind": "administrator",
                "role": None,
            },
            {
                "membership": memberships["viewer@example.com"],
                "address": "viewer@example.com",
                "kind": "member",
                "role": None,
            },
        ],
        "added": [
            {"address": "ops@example.com", "role": None, "eligible": True, "memberships": []}
        ],
    }
    assert body["state"] == "sending"
    (delivery,) = body["deliveries"]
    assert delivery["recipients"] == ["ops@example.com", "admin@example.com", "viewer@example.com"]


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        (
            {"recipients": [f"r{index}@example.com" for index in range(MAX_RECIPIENT_LIMIT + 1)]},
            "recipients",
        ),
        ({"times": [f"{hour:02d}:00" for hour in range(11)]}, "times"),
        ({"surprise": True}, ""),
    ],
)
def test_reminder_input_is_bounded(changes: dict[str, Any], field: str) -> None:
    system = approved()

    response = put(system, **changes)

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert list(response.json().get("fields", {})) == ([field] if field else [])


def test_a_list_saved_under_a_higher_limit_can_be_paused_unchanged() -> None:
    system = approved()
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 150})
    saved = [f"r{index}@example.com" for index in range(101)]
    save(system, recipients=saved)
    NotificationSettings.objects.filter(pk=1).update(max_recipients=5)

    response = put(system, enabled=False, recipients=saved)

    assert response.status_code == 200
    assert (response.json()["enabled"], response.json()["recipient_count"]) == (False, 101)


def test_the_workspace_member_choice_is_required() -> None:
    system = approved()
    values = submission()
    del values["include_workspace_members"]

    response = client_for(system.member).put(url(system), values, content_type="application/json")

    assert response.status_code == 400
    assert list(response.json()["fields"]) == ["include_workspace_members"]


@pytest.mark.parametrize(
    ("changes", "code", "field"),
    [
        ({"times": []}, "reminder_time_required", "times"),
        ({"times": ["17:00", "17:00"]}, "duplicate_reminder_times", "times"),
        (
            {"recipients": ["Ops@example.com", "ops@example.com"]},
            "duplicate_recipients",
            "recipients",
        ),
        ({"recipients": []}, "recipient_required", "recipients"),
        (
            {"recipients": [f"r{index}@example.com" for index in range(6)]},
            "recipient_limit_exceeded",
            "recipients",
        ),
    ],
)
def test_reminder_refusals_name_their_code_and_field(
    changes: dict[str, Any], code: str, field: str
) -> None:
    system = approved()

    response = put(system, **changes)

    assert response.status_code == 400
    assert (response.json()["code"], list(response.json()["fields"])) == (code, [field])


def test_more_times_than_the_provider_quota_name_the_limit() -> None:
    system = approved()
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=2)

    response = put(system, times=["07:00", "12:00", "17:00"])

    assert (response.status_code, response.json()["code"]) == (400, "too_many_reminder_times")
    assert list(response.json()["fields"]) == ["times"]
    assert "2" in response.json()["message"]


def test_turning_reminders_on_needs_an_approved_plant_and_an_enabled_provider() -> None:
    system = approved()
    pending = submit_region(
        system.member,
        system.tenant.pk,
        name="Second site",
        latitude=41,
        longitude=29,
        timezone="Europe/Istanbul",
    )
    pending_url = f"/api/tenants/{system.tenant.pk}/regions/{pending.pk}/reminders/"
    ProviderAccount.objects.filter(pk=system.account.pk).update(enabled=False)

    refusals = [
        client_for(system.member).put(pending_url, submission(), content_type="application/json"),
        put(system),
    ]

    assert [(answer.status_code, answer.json()["code"]) for answer in refusals] == [
        (400, "plant_not_approved"),
        (400, "provider_account_unavailable"),
    ]
    assert [list(answer.json()["fields"]) for answer in refusals] == [["enabled"], ["enabled"]]


def test_an_enabled_schedule_stays_enabled_while_its_provider_needs_attention() -> None:
    system = approved()
    save(system)
    ProviderAccount.objects.filter(pk=system.account.pk).update(enabled=False)

    response = put(system, recipients=["ops@example.com", "plant@example.com"])

    assert response.status_code == 200
    assert (response.json()["enabled"], response.json()["state"]) == (True, "provider_attention")


def test_a_trashed_plant_is_not_found_before_its_input_is_checked() -> None:
    system = approved()
    trash_region(system.member, system.tenant.pk, system.region.pk)

    response = put(system, recipients=["ops@example.com", "OPS@example.com"])

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("arrange", "state"),
    [
        (lambda system: save(system, enabled=False), "paused_by_user"),
        (
            lambda system: (save(system), NotificationSettings.objects.update(max_recipients=1)),
            "sending",
        ),
        (
            lambda system: (
                save(system, recipients=["a@example.com", "b@example.com"]),
                NotificationSettings.objects.update(max_recipients=1),
            ),
            "over_recipient_limit",
        ),
        (
            lambda system: (
                save(system, recipients=[], include_workspace_members=True),
                EmailAddress.objects.update(verified=False),
            ),
            "no_recipients",
        ),
    ],
)
def test_the_reminder_state_names_why_mail_is_or_is_not_sent(arrange: Any, state: str) -> None:
    system = approved()
    NotificationSettings.objects.get_or_create(pk=1)
    arrange(system)

    assert client_for(system.member).get(url(system)).json()["state"] == state


def test_the_members_screen_warns_before_a_new_member_pushes_a_plant_over_its_limit() -> None:
    system = approved()
    viewer_of(system)
    save(system, recipients=["ops@example.com"], include_workspace_members=True)
    members = f"/api/tenants/{system.tenant.pk}/members/"
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 3})

    at_limit = client_for(system.member).get(members).json()
    NotificationSettings.objects.filter(pk=1).update(max_recipients=4)
    below_limit = client_for(system.member).get(members).json()

    assert (at_limit["plants_at_recipient_limit"], below_limit["plants_at_recipient_limit"]) == (
        1,
        0,
    )


def test_the_members_warning_counts_only_plants_a_new_member_would_pause() -> None:
    system = approved()
    viewer_of(system)
    save(system, recipients=["ops@example.com"], include_workspace_members=True)
    members = f"/api/tenants/{system.tenant.pk}/members/"
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 2})

    already_over = client_for(system.member).get(members).json()
    NotificationSettings.objects.filter(pk=1).update(max_recipients=3)
    ProviderAccount.objects.filter(pk=system.account.pk).update(enabled=False)
    held_otherwise = client_for(system.member).get(members).json()

    assert (
        already_over["plants_at_recipient_limit"],
        held_otherwise["plants_at_recipient_limit"],
    ) == (0, 0)


def test_the_members_warning_reads_one_statement_when_no_plant_includes_the_members() -> None:
    system = approved()
    viewer_of(system)
    save(system, recipients=["ops@example.com"])

    with CaptureQueriesContext(connection) as captured:
        assert plants_at_recipient_limit(system.tenant.pk) == 0

    assert len(captured) == 1


def test_concurrent_first_saves_of_one_plant_keep_one_schedule() -> None:
    system = approved()
    start = Barrier(4)

    def save_concurrently(index: int) -> int:
        close_old_connections()
        try:
            start.wait(timeout=10)
            return int(
                put(
                    system, recipients=[f"ops-{index}@example.com"], expected_revision=0
                ).status_code
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(4) as pool:
        statuses = list(pool.map(save_concurrently, range(4)))

    assert sorted(statuses) == [200, 409, 409, 409]
    (schedule,) = ReminderSchedule.objects.all()
    stored = client_for(system.member).get(url(system)).json()["addresses"]["recipients"]
    assert stored in [[f"ops-{index}@example.com"] for index in range(4)]
    assert schedule.region_id == system.region.pk


def test_a_save_after_a_quota_reduction_keeps_the_stored_times() -> None:
    system = approved()
    save(system, times=["07:00", "12:00", "17:00"])
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=2)

    response = put(system, times=["07:00", "12:00", "17:00"], recipients=["new@example.com"])

    assert response.status_code == 200
    assert response.json()["times"] == ["07:00", "12:00", "17:00"]


def test_turning_reminders_on_needs_the_plant_revision_it_was_approved_at() -> None:
    system = approved()
    save(system, enabled=False)
    Region.objects.filter(pk=system.region.pk).update(revision=F("revision") + 1)

    response = put(system)

    assert (response.status_code, response.json()["code"]) == (400, "plant_not_approved")


@pytest.mark.parametrize(
    ("change", "state"),
    [
        ({"status": "pending"}, "waiting_for_approval"),
        ({"revision": 99}, "waiting_for_approval"),
    ],
)
def test_an_enabled_schedule_of_a_plant_out_of_approval_waits(
    change: dict[str, Any], state: str
) -> None:
    system = approved()
    save(system)
    Region.objects.filter(pk=system.region.pk).update(**change)

    assert client_for(system.member).get(url(system)).json()["state"] == state


def test_an_invalid_saved_recipient_names_its_state() -> None:
    system = approved()
    schedule = save(system)
    store(schedule, [*schedule_addresses(schedule), "not-an-address"])

    assert client_for(system.member).get(url(system)).json()["state"] == "invalid_recipient"
