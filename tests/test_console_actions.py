import json
from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import (
    Language,
    Membership,
    Tenant,
    UserLanguagePreference,
    WorkspaceInvitation,
    WorkspaceRole,
)
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import HistoricalReferenceImport, RequestAttempt
from pyracast.forecasts.validation import validate_binding
from pyracast.metering.models import EpiasConnection
from pyracast.notifications.models import NotificationSettings
from pyracast.plants.models import Plant
from pyracast.providers import SolcastProvider
from pyracast.regions.models import (
    LocationChangeStatus,
    Region,
    RegionLocationChangeRequest,
    RegionStatus,
)
from pyracast.regions.operations import request_location_change
from tests.support.admin_setup import admin_system, review_fields, setup_input, submitted_plant
from tests.support.api import verified_admin
from tests.support.domain import FakeAdapter, System
from tests.support.historical_reference_import import system_for_import
from tests.support.metering import PROVIDER_PLANT, FixtureClient
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)


def plant_url(region: Region) -> str:
    return reverse("admin:workspace_plant", args=(region.pk,))


def binding_action(system: System, action: str, **changes: object) -> dict[str, object]:
    return {
        "action": action,
        "expected_revision": system.region.revision,
        "binding_id": system.binding.pk,
        "credential_revision": system.account.credential_revision,
        **review_fields(system),
        **changes,
    }


def validated(system: System) -> System:
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), timezone.now())
    return system


def test_configuring_a_plant_saves_its_provider_setup_and_returns_to_the_plant() -> None:
    system = admin_system()
    client, _ = verified_admin()

    response = client.post(plant_url(system.region), setup_input(system, site_id="moved-site"))

    assert (response.status_code, response["Location"]) == (302, plant_url(system.region))
    system.region.refresh_from_db()
    current = SiteBinding.objects.get(region=system.region, region_revision=system.region.revision)
    assert current.site_id == "moved-site"


def test_validating_a_plant_spends_one_request_and_returns_to_the_plant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = admin_system()
    monkeypatch.setattr(SolcastProvider, "validate", lambda self, binding: None)
    client, _ = verified_admin()

    response = client.post(plant_url(system.region), binding_action(system, "validate"))

    assert (response.status_code, response["Location"]) == (302, plant_url(system.region))
    assert RequestAttempt.objects.get().outcome == "succeeded"
    system.binding.refresh_from_db()
    assert system.binding.validated_at is not None


def test_approving_a_plant_records_the_approval_and_returns_to_the_plant() -> None:
    system = validated(admin_system())
    submitted_plant(system)
    client, _ = verified_admin()

    response = client.post(plant_url(system.region), binding_action(system, "approve"))

    assert (response.status_code, response["Location"]) == (302, plant_url(system.region))
    system.region.refresh_from_db()
    assert system.region.status == RegionStatus.APPROVED


def test_clearing_a_provider_delay_removes_it_and_returns_to_the_account() -> None:
    system = admin_system()
    blocked_until = timezone.now() + timedelta(hours=2)
    ProviderAccount.objects.filter(pk=system.account.pk).update(request_blocked_until=blocked_until)
    client, _ = verified_admin()
    account_url = reverse("admin:workspace_provider_account", args=(system.account.pk,))

    response = client.post(
        f"{account_url}delay/",
        {
            "expected_revision": system.account.credential_revision,
            "expected_blocked_until": blocked_until.isoformat(),
            "provider_recovered": "on",
        },
    )

    assert (response.status_code, response["Location"]) == (302, account_url)
    assert ProviderAccount.objects.get(pk=system.account.pk).request_blocked_until is None


def test_connecting_and_disconnecting_epias_store_the_connection_and_return_to_the_plant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    monkeypatch.setattr("pyracast.metering.operations.get_epias_client", FixtureClient)
    client, _ = verified_admin()
    url = plant_url(system.region)

    connected = client.post(
        url,
        {
            "action": "epias_connect",
            "epias-provider_plant_id": 7,
            "epias-expected_eic": PROVIDER_PLANT.eic,
        },
    )
    assert (connected.status_code, connected["Location"]) == (302, url)
    assert EpiasConnection.objects.get(plant=plant).enabled

    disconnected = client.post(url, {"action": "epias_disconnect"})
    assert (disconnected.status_code, disconnected["Location"]) == (302, url)
    assert not EpiasConnection.objects.get(plant=plant).enabled


def decision(region: Region, action: str, **changes: object) -> dict[str, object]:
    return {
        "expected_revision": region.revision,
        "expected_status": region.status,
        "action": action,
        **changes,
    }


@pytest.mark.parametrize(
    ("approved", "action", "status"),
    [(False, "reject", RegionStatus.REJECTED), (True, "suspend", RegionStatus.SUSPENDED)],
)
def test_a_review_decision_changes_the_status_and_returns_to_the_plant(
    monkeypatch: pytest.MonkeyPatch, approved: bool, action: str, status: str
) -> None:
    system = make_plant(monkeypatch)[0] if approved else admin_system()
    system.region.refresh_from_db()
    client, _ = verified_admin()

    response = client.post(
        reverse("admin:workspace_decision", args=(system.region.pk,)),
        decision(system.region, action),
    )

    assert (response.status_code, response["Location"]) == (302, plant_url(system.region))
    system.region.refresh_from_db()
    assert system.region.status == status


@pytest.mark.parametrize(
    ("action", "status"),
    [
        ("approve_location", LocationChangeStatus.APPROVED),
        ("reject_location", LocationChangeStatus.REJECTED),
    ],
)
def test_a_location_decision_settles_the_request_and_returns_to_the_plant(
    monkeypatch: pytest.MonkeyPatch, action: str, status: str
) -> None:
    system, _ = make_plant(monkeypatch)
    change = request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=system.region.revision,
        latitude=39.6,
        longitude=28.6,
        timezone="Europe/Istanbul",
    )
    system.region.refresh_from_db()
    client, _ = verified_admin()

    response = client.post(
        reverse("admin:workspace_decision", args=(system.region.pk,)),
        decision(system.region, action, location_change_id=change.pk),
    )

    assert (response.status_code, response["Location"]) == (302, plant_url(system.region))
    assert RegionLocationChangeRequest.objects.get(pk=change.pk).status == status


def site_fields(**changes: object) -> dict[str, object]:
    return {
        "name": "Console plant",
        "latitude": "41.0082",
        "longitude": "28.9784",
        "timezone": "Europe/Istanbul",
        "dc_capacity_mw": "1.5",
        "measurement_basis": "net_export",
        "mounting_type": "unknown",
        **changes,
    }


def test_editing_plant_details_saves_them_and_returns_to_the_plant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    client, _ = verified_admin()
    url = reverse("admin:workspace_plant_details", args=(system.region.pk,))
    form = client.get(url).context["form"]

    response = client.post(
        url,
        {
            **{name: form[name].value() for name in form.fields},
            "name": "Renamed plant",
            "reason": "",
        },
    )

    assert (response.status_code, response["Location"]) == (302, plant_url(system.region))
    plant.refresh_from_db()
    assert plant.name == "Renamed plant"


def history_file() -> SimpleUploadedFile:
    rows = [{"period_end": "2026-11-07T23:00:00+02:00", "period": "PT30M", "pv_power_rooftop": 0.5}]
    return SimpleUploadedFile(
        "history.json",
        json.dumps({"estimated_actuals": rows}).encode(),
        content_type="application/json",
    )


def test_a_historical_preview_stores_nothing_and_its_save_returns_to_the_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    Plant.objects.create(region=system.region, name="Reference plant")
    client, _ = verified_admin()
    url = reverse("admin:workspace_historical_import", args=(system.region.pk,))

    preview = client.post(url, {"action": "preview", "file": history_file()})
    assert preview.status_code == 200
    assert not HistoricalReferenceImport.objects.exists()

    saved = client.post(
        url,
        {
            "action": "save",
            "file": history_file(),
            "preview_token": preview.context["preview_token"],
            "settings_confirmed": "on",
        },
    )
    assert (saved.status_code, saved["Location"]) == (302, url)
    assert HistoricalReferenceImport.objects.count() == 1


def test_saving_settings_stores_the_limit_and_returns_to_settings() -> None:
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 5})
    client, _ = verified_admin()
    url = reverse("admin:workspace_settings")

    response = client.post(url, {"max_recipients": 8, "expected_max_recipients": 5})

    assert (response.status_code, response["Location"]) == (302, url)
    assert NotificationSettings.objects.get().max_recipients == 8


def test_choosing_a_language_stores_it_and_returns_to_the_console_page() -> None:
    client, user = verified_admin()
    page = reverse("admin:workspace_plants")

    response = client.post(reverse("admin:workspace_language"), {"language": "tr", "next": page})

    assert (response.status_code, response["Location"]) == (302, page)
    assert UserLanguagePreference.objects.get(user=user).language == Language.TR


def test_signing_out_ends_the_session_and_opens_the_application() -> None:
    client, _ = verified_admin()

    response = client.post(reverse("admin:logout"))

    assert (response.status_code, response["Location"]) == (302, "/app/")
    assert client.get("/api/session/").json()["user"] is None


def workspace() -> tuple[Tenant, User, User]:
    tenant = Tenant.objects.create(name="Solar workspace")
    owner = User.objects.create_user(username="owner", email="owner@example.com")
    viewer = User.objects.create_user(username="viewer", email="viewer@example.com")
    Membership.objects.create(tenant=tenant, user=owner, role=WorkspaceRole.ADMIN)
    Membership.objects.create(tenant=tenant, user=viewer)
    return tenant, owner, viewer


def tenant_url(tenant: Tenant, section: str = "") -> str:
    return reverse("admin:workspace_tenant", args=(tenant.pk,)) + section


def test_creating_a_workspace_stores_it_and_opens_it() -> None:
    client, _ = verified_admin()

    response = client.post(
        reverse("admin:workspace_tenant_new"),
        {"name": "New workspace", "administrator_email": "", "language": "en"},
    )

    tenant = Tenant.objects.get(name="New workspace")
    assert (response.status_code, response["Location"]) == (302, tenant_url(tenant))


def test_renaming_a_workspace_stores_the_name_and_opens_it() -> None:
    tenant, _, _ = workspace()
    client, _ = verified_admin()

    response = client.post(
        reverse("admin:workspace_tenant_name", args=(tenant.pk,)),
        {"name": "Renamed workspace", "expected_name": tenant.name},
    )

    assert (response.status_code, response["Location"]) == (302, tenant_url(tenant))
    assert Tenant.objects.get(pk=tenant.pk).name == "Renamed workspace"


def test_changing_the_administrator_stores_the_role_and_opens_the_members() -> None:
    tenant, owner, viewer = workspace()
    client, _ = verified_admin()

    response = client.post(
        reverse("admin:workspace_tenant_administrator", args=(tenant.pk,)),
        {"administrator": viewer.pk, "expected_admin_id": owner.pk},
    )

    assert (response.status_code, response["Location"]) == (
        302,
        tenant_url(tenant, "#members-title"),
    )
    assert Membership.objects.get(user=viewer).role == WorkspaceRole.ADMIN


def test_inviting_a_member_stores_the_invitation_and_opens_the_invitations() -> None:
    tenant, _, _ = workspace()
    client, _ = verified_admin()

    response = client.post(
        reverse("admin:workspace_invitation_new", args=(tenant.pk,)),
        {"email": "guest@example.com", "role": WorkspaceRole.VIEWER, "language": "en"},
    )

    assert (response.status_code, response["Location"]) == (
        302,
        tenant_url(tenant, "#invitations-title"),
    )
    assert WorkspaceInvitation.objects.get(tenant=tenant).email == "guest@example.com"


def test_reissuing_and_revoking_an_invitation_store_it_and_open_the_invitations() -> None:
    tenant, _, _ = workspace()
    client, operator = verified_admin()
    invitation = invite_workspace_member(operator, tenant.pk, "guest@example.com", platform=True)
    WorkspaceInvitation.objects.update(created_at=timezone.now() - timedelta(minutes=2))
    invitations = tenant_url(tenant, "#invitations-title")

    reissued = client.post(
        reverse("admin:workspace_invitation_reissue", args=(tenant.pk, invitation.pk))
    )
    assert (reissued.status_code, reissued["Location"]) == (302, invitations)
    replacement = WorkspaceInvitation.objects.latest("pk")
    assert replacement.pk != invitation.pk

    revoked = client.post(
        reverse("admin:workspace_invitation_revocation", args=(tenant.pk, replacement.pk))
    )
    assert (revoked.status_code, revoked["Location"]) == (302, invitations)
    replacement.refresh_from_db()
    assert replacement.state_at(timezone.now()) == "revoked"


def test_removing_a_member_deletes_the_membership_and_opens_the_members() -> None:
    tenant, _, viewer = workspace()
    client, _ = verified_admin()

    response = client.post(reverse("admin:workspace_member_removal", args=(tenant.pk, viewer.pk)))

    assert (response.status_code, response["Location"]) == (
        302,
        tenant_url(tenant, "#members-title"),
    )
    assert not Membership.objects.filter(user=viewer).exists()


def test_adding_a_plant_stores_it_and_opens_its_review() -> None:
    tenant, _, _ = workspace()
    client, _ = verified_admin()

    response = client.post(reverse("admin:workspace_plant_new", args=(tenant.pk,)), site_fields())

    region = Region.objects.get(tenant=tenant)
    assert (response.status_code, response["Location"]) == (302, plant_url(region))
    assert Plant.objects.get(region=region).name == "Console plant"


def test_read_pages_answer_without_storing_anything() -> None:
    client, _ = verified_admin()

    pages: list[Any] = [
        client.get(reverse(name))
        for name in (
            "admin:index",
            "admin:workspace_plants",
            "admin:workspace_operations",
            "admin:workspace_settings",
            "admin:workspace_tenants",
            "admin:workspace_confirm",
        )
    ]

    assert [page.status_code for page in pages] == [200] * len(pages)
    assert not Tenant.objects.exists()
