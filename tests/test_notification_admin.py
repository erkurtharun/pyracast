import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from pyracast.notifications.models import NotificationSettings
from tests.test_api import verified_admin
from tests.test_domain import make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_stock_settings_page_is_read_only_and_members_see_the_global_limit() -> None:
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 8})
    client, _ = verified_admin()
    url = reverse("admin:notifications_notificationsettings_change", args=(1,))
    response = client.get(url)
    assert response.status_code == 200
    assert b'name="max_recipients"' not in response.content
    assert client.post(url, {"max_recipients": 9, "_save": "Save"}).status_code == 403
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
    assert NotificationSettings.objects.get().max_recipients == 8
    system = make_system()
    member = Client()
    member.force_login(system.member)
    response = member.get(f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/")
    assert response.status_code == 200
    assert response.json()["recipient_limit"] == 8


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
