import pytest
from allauth.account.models import EmailAddress
from django.test import Client

from pyracast.notifications.models import ReminderSchedule
from tests.support.domain import make_system

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def gate_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyracast.notifications.serializers.VISIBLE_MODE_RELEASED", False)


def test_until_the_release_a_visible_mode_is_refused_and_nothing_is_stored() -> None:
    system = make_system(approved=True)
    EmailAddress.objects.create(
        user=system.member, email="admin@example.com", verified=True, primary=True
    )
    client = Client()
    client.force_login(system.member)
    body = {
        "enabled": True,
        "include_workspace_members": True,
        "language": "en",
        "recipients": ["ops@example.com"],
        "times": ["17:00"],
        "expected_revision": 0,
    }
    url = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/"

    refused = client.put(url, body | {"hide_recipients": False}, content_type="application/json")

    assert refused.status_code == 400
    assert refused.json()["code"] == "invalid_request"
    assert list(refused.json()["fields"]) == ["hide_recipients"]
    assert not ReminderSchedule.objects.exists()

    saved = client.put(url, body | {"hide_recipients": True}, content_type="application/json")

    assert saved.status_code == 200
    assert saved.json()["hide_recipients"] is True
