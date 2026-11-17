import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from pyracast.notifications.models import NotificationSettings
from tests.support.api import verified_admin
from tests.support.domain import make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_admin_updates_global_limit_seen_by_members_and_cannot_duplicate_or_delete() -> None:
    settings, _ = NotificationSettings.objects.get_or_create(pk=1)
    assert settings.max_recipients == 5
    client, _ = verified_admin()
    url = reverse("admin:notifications_notificationsettings_change", args=(1,))
    response = client.get(url)
    assert response.status_code == 200
    assert b'name="max_recipients"' in response.content
    assert client.post(url, {"max_recipients": 8, "_save": "Save"}).status_code == 302
    settings.refresh_from_db()
    assert settings.max_recipients == 8
    system = make_system()
    member = Client()
    member.force_login(system.member)
    response = member.get(f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/")
    assert response.status_code == 200
    assert response.json()["recipient_limit"] == 8
    assert (
        client.post(
            reverse("admin:notifications_notificationsettings_add"), {"max_recipients": 12}
        ).status_code
        == 403
    )
    assert (
        client.post(
            reverse("admin:notifications_notificationsettings_delete", args=(1,)), {"post": "yes"}
        ).status_code
        == 403
    )
    assert NotificationSettings.objects.count() == 1


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_admin_rejects_invalid_recipient_limits(value: str) -> None:
    settings, _ = NotificationSettings.objects.get_or_create(pk=1)
    client, _ = verified_admin()
    response = client.post(
        reverse("admin:notifications_notificationsettings_change", args=(1,)),
        {"max_recipients": value, "_save": "Save"},
    )
    assert response.status_code == 200
    settings.refresh_from_db()
    assert settings.max_recipients == 5


def test_admin_can_initialize_missing_settings_but_requires_csrf_for_writes() -> None:
    NotificationSettings.objects.all().delete()
    client, _ = verified_admin()
    url = reverse("admin:notifications_notificationsettings_add")
    assert client.get(url).status_code == 200
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.cookies = client.cookies
    assert csrf_client.post(url, {"max_recipients": 7}).status_code == 403
    assert not NotificationSettings.objects.exists()
    assert client.post(url, {"max_recipients": 7, "_save": "Save"}).status_code == 302
    assert NotificationSettings.objects.get(pk=1).max_recipients == 7


@pytest.mark.parametrize("identity", ["anonymous", "member", "staff", "unverified_admin"])
def test_reminder_limit_settings_require_verified_platform_admin(identity: str) -> None:
    settings, _ = NotificationSettings.objects.get_or_create(pk=1)
    client = Client()
    if identity != "anonymous":
        user = User.objects.create_user(
            username=identity,
            is_staff=identity in {"staff", "unverified_admin"},
            is_superuser=identity == "unverified_admin",
        )
        client.force_login(user)
    url = reverse("admin:notifications_notificationsettings_change", args=(1,))
    assert client.get(url).status_code == 302
    assert client.post(url, {"max_recipients": 50}).status_code == 302
    settings.refresh_from_db()
    assert settings.max_recipients == 5
