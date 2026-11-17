from typing import Any

import pytest
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from pyracast.accounts.models import Membership
from pyracast.epias import EpiasError
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection
from pyracast.plants.models import Plant, PlantEvent
from tests.support.admin_setup import admin_system, submitted_plant
from tests.support.api import verified_admin
from tests.support.metering import PROVIDER_PLANT, FixtureClient
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)


def connection_input(**changes: Any) -> dict[str, Any]:
    return {
        "action": "epias_connect",
        "epias-provider_plant_id": 7,
        "epias-expected_eic": PROVIDER_PLANT.eic,
        **changes,
    }


def test_admin_connects_without_membership_and_disconnects_after_approval_is_withdrawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    provider = FixtureClient()
    monkeypatch.setattr("pyracast.metering.operations.get_epias_client", lambda: provider)
    client, actor = verified_admin()
    assert not Membership.objects.filter(user=actor).exists()
    url = reverse("admin:workspace_plant", args=(system.region.pk,))
    before = client.get(url)
    assert before.status_code == 200
    assert b"Not connected to EP" in before.content
    assert b'id="id_epias-provider_plant_id"' in before.content
    assert b'id="id_epias-expected_eic"' in before.content
    assert provider.calls == 0
    response = client.post(url, connection_input())
    assert response.status_code == 302
    assert response.headers["Location"] == url
    connection = EpiasConnection.objects.get(plant=plant)
    assert connection.enabled
    assert connection.connected_by == actor
    assert connection.provider_plant_id == 7
    assert connection.eic == PROVIDER_PLANT.eic
    assert EpiasCollectionJob.objects.filter(connection=connection, status="pending").count() == 90
    assert PlantEvent.objects.get(plant=plant, action="epias_connected").actor == actor
    page = client.get(url)
    assert b"Connected" in page.content
    assert b"Solar plant" in page.content
    assert b"40X0000000000001" in page.content
    assert "<summary>Change EPİAŞ connection</summary>".encode() in page.content
    assert provider.calls == 1
    system.region.status = "suspended"
    system.region.save(update_fields=("status",))
    page = client.get(url)
    assert page.context["can_connect_epias"] is False
    assert "Disconnect EPİAŞ".encode() in page.content
    response = client.post(url, {"action": "epias_disconnect"})
    assert response.status_code == 302
    disconnected = EpiasConnection.objects.get(pk=connection.pk)
    assert not disconnected.enabled
    assert not EpiasCollectionJob.objects.filter(status__in=("pending", "running")).exists()
    assert PlantEvent.objects.get(plant=plant, action="epias_disconnected").actor == actor
    page = client.get(url)
    assert b"Disconnected" in page.content
    assert "Disconnect EPİAŞ".encode() not in page.content
    assert provider.calls == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"epias-provider_plant_id": 0},
        {"epias-provider_plant_id": "not-an-id"},
        {"epias-expected_eic": "short"},
        {"epias-expected_eic": "40X000000000000!"},
    ],
)
def test_invalid_epias_fields_never_reach_provider(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, object]
) -> None:
    system, _ = make_plant(monkeypatch)
    provider = FixtureClient()
    monkeypatch.setattr("pyracast.metering.operations.get_epias_client", lambda: provider)
    client, _ = verified_admin()
    data = connection_input(**changes)
    response = client.post(reverse("admin:workspace_plant", args=(system.region.pk,)), data)
    assert response.status_code == 400
    assert response.context["epias_form"].errors
    for field, value in data.items():
        assert response.context["epias_form"].data[field] == str(value)
    assert not response.context["action_form"].is_bound
    assert provider.calls == 0
    assert not EpiasConnection.objects.exists()
    assert not EpiasCollectionJob.objects.exists()


@pytest.mark.parametrize(
    "error", ["provider", "credentials", "mismatch", "configuration", "missing"]
)
def test_connection_errors_are_safe_and_preserve_input(
    monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    system, _ = make_plant(monkeypatch)
    provider_detail = "private-password-and-provider-response"

    def unavailable() -> None:
        raise EpiasError("credentials_unavailable" if error == "credentials" else provider_detail)

    provider = FixtureClient(unavailable if error in {"provider", "credentials"} else lambda: None)
    monkeypatch.setattr("pyracast.metering.operations.get_epias_client", lambda: provider)
    if error == "missing":

        def removed_plant(*args: object, **kwargs: object) -> None:
            raise Plant.DoesNotExist(provider_detail)

        monkeypatch.setattr("pyracast.admin.connect_epias", removed_plant)
    if error == "configuration":
        system.region.status = "suspended"
        system.region.save(update_fields=("status",))
    data = connection_input(**{"epias-provider_plant_id": 99})
    client, _ = verified_admin()
    response = client.post(reverse("admin:workspace_plant", args=(system.region.pk,)), data)
    assert response.status_code == 400
    for field, value in data.items():
        assert response.context["epias_form"].data[field] == str(value)
    expected = {
        "provider": "EPİAŞ is currently unavailable",
        "credentials": "EPİAŞ service credentials are unavailable",
        "mismatch": "plant ID and EIC did not match",
        "configuration": "plant configuration is unavailable or changed",
        "missing": "plant configuration is unavailable or changed",
    }
    assert expected[error].encode() in response.content
    assert provider_detail.encode() not in response.content
    assert not EpiasConnection.objects.exists()
    assert not EpiasCollectionJob.objects.exists()
    assert provider.calls == (0 if error in {"configuration", "missing"} else 1)


@pytest.mark.parametrize("action", ["epias_connect", "epias_disconnect"])
@pytest.mark.parametrize(
    "identity",
    ["anonymous", "member", "staff", "unverified_admin", "inactive", "revoked_mfa"],
)
def test_connection_mutations_require_live_verified_admin(
    monkeypatch: pytest.MonkeyPatch, action: str, identity: str
) -> None:
    system, plant = make_plant(monkeypatch)
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=8,
        eic="40X0000000000002",
        name="Saved plant",
        connected_by=system.admin,
    )
    provider = FixtureClient()
    monkeypatch.setattr("pyracast.metering.operations.get_epias_client", lambda: provider)
    client = Client()
    if identity in {"inactive", "revoked_mfa"}:
        client, actor = verified_admin()
        if identity == "inactive":
            actor.is_active = False
            actor.save(update_fields=("is_active",))
        else:
            Authenticator.objects.filter(user=actor).delete()
    elif identity == "member":
        client.force_login(system.member)
    elif identity != "anonymous":
        actor = User.objects.create_user(
            username=identity, is_staff=True, is_superuser=identity == "unverified_admin"
        )
        client.force_login(actor)
    response = client.post(
        reverse("admin:workspace_plant", args=(system.region.pk,)),
        connection_input(action=action),
    )
    assert response.status_code == 302
    assert provider.calls == 0
    connection.refresh_from_db()
    assert connection.enabled
    assert connection.provider_plant_id == 8
    assert connection.eic == "40X0000000000002"
    assert not PlantEvent.objects.filter(action__startswith="epias_").exists()


@pytest.mark.parametrize("action", ["epias_connect", "epias_disconnect"])
def test_connection_mutations_enforce_csrf(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    system, plant = make_plant(monkeypatch)
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=8,
        eic="40X0000000000002",
        name="Saved plant",
        connected_by=system.admin,
    )
    provider = FixtureClient()
    monkeypatch.setattr("pyracast.metering.operations.get_epias_client", lambda: provider)
    client, _ = verified_admin(enforce_csrf_checks=True)
    response = client.post(
        reverse("admin:workspace_plant", args=(system.region.pk,)),
        connection_input(action=action),
    )
    assert response.status_code == 403
    assert provider.calls == 0
    connection.refresh_from_db()
    assert connection.enabled
    assert connection.provider_plant_id == 8
    assert not PlantEvent.objects.filter(action__startswith="epias_").exists()


def test_pending_or_missing_plant_disables_connection_without_provider_io() -> None:
    system = admin_system()
    client, _ = verified_admin()
    url = reverse("admin:workspace_plant", args=(system.region.pk,))
    page = client.get(url)
    assert page.context["can_connect_epias"] is False
    assert client.post(url, connection_input()).status_code == 400
    submitted_plant(system)
    page = client.get(url)
    assert page.context["can_connect_epias"] is False
    assert b"before connecting" in page.content
    assert client.post(url, connection_input()).status_code == 400
    assert not EpiasConnection.objects.exists()
    assert client.put(url).status_code == 405
