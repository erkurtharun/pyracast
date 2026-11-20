import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from pyracast.accounts.models import Membership, Tenant, WorkspaceAdminChange, WorkspaceRole
from pyracast.notifications.models import NotificationSettings
from tests.support.api import verified_admin

pytestmark = pytest.mark.django_db

LIMIT_CHANGED = "Recipient limit changed from 5 to 9."
LIMIT_KEPT = "The recipient limit is already 9."
LIMIT_UNCHANGED = "The recipient limit is already 5."
NOW_ADMINISTRATOR = "viewer@example.com is now the workspace administrator."
ALREADY_ADMINISTRATOR = "viewer@example.com is already the workspace administrator."
OWNER_ALREADY_ADMINISTRATOR = "owner@example.com is already the workspace administrator."


def notices(client: Client, url: str, data: dict[str, object]) -> tuple[int, list[str]]:
    response = client.post(url, data, follow=True)
    status = response.redirect_chain[0][1] if response.redirect_chain else response.status_code
    return status, [str(message) for message in response.context["messages"]]


def workspace() -> tuple[Tenant, User, User]:
    tenant = Tenant.objects.create(name="Solar workspace")
    owner = User.objects.create_user(username="owner", email="owner@example.com")
    viewer = User.objects.create_user(username="viewer", email="viewer@example.com")
    Membership.objects.create(tenant=tenant, user=owner, role=WorkspaceRole.ADMIN)
    Membership.objects.create(tenant=tenant, user=viewer)
    return tenant, owner, viewer


def test_saving_the_current_recipient_limit_says_nothing_changed() -> None:
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 5})
    client, _ = verified_admin()
    url = reverse("admin:workspace_settings")

    outcome = notices(client, url, {"max_recipients": 5, "expected_max_recipients": 5})

    assert outcome == (302, [LIMIT_UNCHANGED])
    assert not LogEntry.objects.exists()


def test_a_double_submitted_recipient_limit_reports_the_change_once() -> None:
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 5})
    client, _ = verified_admin()
    url = reverse("admin:workspace_settings")
    change: dict[str, object] = {"max_recipients": 9, "expected_max_recipients": 5}

    assert notices(client, url, change) == (302, [LIMIT_CHANGED])
    assert notices(client, url, change) == (302, [LIMIT_KEPT])
    assert LogEntry.objects.count() == 1
    assert NotificationSettings.objects.get(pk=1).max_recipients == 9


def test_a_double_submitted_reassignment_reports_the_change_once() -> None:
    tenant, owner, viewer = workspace()
    client, _ = verified_admin()
    url = reverse("admin:workspace_tenant_administrator", args=(tenant.pk,))
    change: dict[str, object] = {"administrator": viewer.pk, "expected_admin_id": owner.pk}

    assert notices(client, url, change) == (302, [NOW_ADMINISTRATOR])
    assert notices(client, url, change) == (302, [ALREADY_ADMINISTRATOR])
    assert WorkspaceAdminChange.objects.count() == 1


def test_choosing_the_current_administrator_says_nothing_changed() -> None:
    tenant, owner, _ = workspace()
    client, _ = verified_admin()
    url = reverse("admin:workspace_tenant_administrator", args=(tenant.pk,))

    outcome = notices(client, url, {"administrator": owner.pk, "expected_admin_id": owner.pk})

    assert outcome == (302, [OWNER_ALREADY_ADMINISTRATOR])
    assert not WorkspaceAdminChange.objects.exists()
