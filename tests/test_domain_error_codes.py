from datetime import timedelta
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant
from pyracast.errors import DomainError
from pyracast.incidents.operations import finish_incident, save_incident
from pyracast.irradiance import local_day_bounds
from pyracast.plants.operations import create_plant
from tests.support.domain import make_system
from tests.support.plant_operations import make_plant
from tests.support.provisional_measurements import DAY
from tests.support.workspaces import invitation_token

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def signed_in(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def post(client: Client, path: str, data: dict[str, Any], language: str = "en") -> Any:
    return client.post(
        path, data, content_type="application/json", headers={"Accept-Language": language}
    )


def test_outage_rules_answer_their_own_codes_and_fields() -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="PV", dc_capacity_mw=1
    )
    client = signed_in(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/outages/"
    now = timezone.now()
    outage = {
        "kind": "panels",
        "title": "Panels",
        "starts_at": (now - timedelta(hours=2)).isoformat(),
    }

    future = post(client, path, {**outage, "starts_at": (now + timedelta(hours=1)).isoformat()})
    assert future.status_code == 400
    assert future.json() == {
        "code": "incident_starts_in_future",
        "message": "The outage start must not be in the future.",
        "fields": {"starts_at": "The outage start must not be in the future."},
    }
    backwards = post(client, path, {**outage, "ends_at": (now - timedelta(hours=3)).isoformat()})
    assert backwards.json()["code"] == "incident_ends_before_start"
    assert set(backwards.json()["fields"]) == {"ends_at"}
    later = post(client, path, {**outage, "ends_at": (now + timedelta(hours=1)).isoformat()})
    assert later.json()["code"] == "incident_ends_in_future"
    capacity = post(client, path, {**outage, "available_dc_mw": 2}, language="tr")
    assert capacity.status_code == 400
    assert capacity.json() == {
        "code": "incident_capacity_exceeded",
        "message": (
            "Kullan\u0131labilir kapasite yap\u0131land\u0131r\u0131lm\u0131ş "
            "kapasiteyi aşmamal\u0131d\u0131r."
        ),
        "fields": {
            "available_dc_mw": (
                "Kullan\u0131labilir kapasite "
                "yap\u0131land\u0131r\u0131lm\u0131ş kapasiteyi aşmamal\u0131d\u0131r."
            ),
        },
    }

    created = post(client, path, outage)
    assert created.status_code == 201
    overlap = post(client, path, outage)
    assert overlap.status_code == 400
    assert overlap.json()["code"] == "incident_overlap"
    item = f"{path}{created.json()['id']}/"
    stale = client.put(item, {**outage, "expected_revision": 5}, content_type="application/json")
    assert stale.status_code == 409
    assert stale.json() == {
        "code": "incident_revision_changed",
        "message": "The outage changed. Reload the list and try again.",
    }
    assert post(client, item + "cancel/", {"expected_revision": 1}).status_code == 200
    closed = post(client, item + "cancel/", {"expected_revision": 2})
    assert closed.status_code == 400
    assert closed.json() == {"code": "incident_closed", "message": "This outage is already closed."}


def test_location_change_refusals_answer_their_own_codes() -> None:
    system = make_system(approved=True)
    client = signed_in(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/location-change-requests/"
    revision = system.region.revision
    current = {"latitude": 40, "longitude": 29, "timezone": "Europe/Istanbul"}
    stale = post(client, path, {**current, "latitude": 41, "expected_revision": revision + 1})
    assert stale.status_code == 409
    assert stale.json() == {
        "code": "region_revision_changed",
        "message": "The plant location changed. Reopen the plant and try again.",
    }
    unchanged = post(client, path, {**current, "expected_revision": revision})
    assert unchanged.status_code == 400
    assert unchanged.json()["code"] == "location_unchanged"
    assert (
        post(client, path, {**current, "latitude": 41, "expected_revision": revision}).status_code
        == 201
    )
    pending = post(client, path, {**current, "latitude": 42, "expected_revision": revision})
    assert pending.status_code == 409
    assert pending.json()["code"] == "location_change_already_pending"


@pytest.mark.parametrize(
    ("updates", "field"),
    [
        ({"dc_capacity_mw": 0}, "dc_capacity_mw"),
        ({"surface_azimuth": 360}, "surface_azimuth"),
        ({"latitude": True}, "latitude"),
        ({"timezone": "Mars/Olympus"}, "timezone"),
        ({"mounting_type": "tracking"}, "mounting_type"),
    ],
)
def test_site_input_errors_name_the_rejected_field(updates: dict[str, Any], field: str) -> None:
    system = make_system()
    site = {
        "name": "New plant",
        "latitude": 41.5,
        "longitude": 30.5,
        "timezone": "Europe/Istanbul",
        "dc_capacity_mw": 120,
        "mounting_type": "fixed",
        "surface_tilt": 25,
        "surface_azimuth": 180,
    }
    response = post(
        signed_in(system.member), f"/api/tenants/{system.tenant.pk}/sites/", {**site, **updates}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert list(response.json()["fields"]) == [field]


def test_regional_forecast_outside_its_range_is_coded() -> None:
    system = make_system(approved=True)
    response = signed_in(system.member).get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/?date=2000-01-01",
        headers={"Accept-Language": "tr"},
    )
    assert response.status_code == 400
    assert response.json() == {
        "code": "date_out_of_range",
        "message": "Kullan\u0131labilir tahmin aral\u0131ğ\u0131nda bir tarih seçin.",
    }


def test_stale_production_correction_is_a_coded_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    path = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/"
        "production/changes/"
    )
    payload = {
        "configuration_id": plant.current_configuration_id,
        "unit": "MWh",
        "changes": [
            {
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
                "expected_revision_id": None,
                "action": "set",
                "energy": 2,
            }
        ],
    }
    client = signed_in(system.member)
    assert post(client, path, payload).status_code == 200
    stale = post(client, path, payload)
    assert stale.status_code == 409
    assert stale.json() == {
        "code": "production_revision_changed",
        "message": "Production changed. Reload the latest values before saving.",
    }


def test_workspace_refusals_answer_their_own_codes() -> None:
    tenant = Tenant.objects.create(name="Solar workspace")
    admin = User.objects.create_user(username="admin", email="admin@example.com")
    Membership.objects.create(tenant=tenant, user=admin, role=Membership.Role.ADMIN)
    client = signed_in(admin)

    removal = client.delete(f"/api/tenants/{tenant.pk}/members/{admin.pk}/")
    assert removal.status_code == 400
    assert removal.json() == {
        "code": "workspace_admin_not_removable",
        "message": "The workspace administrator cannot be removed.",
    }
    existing = post(client, f"/api/tenants/{tenant.pk}/invitations/", {"email": admin.email})
    assert existing.status_code == 400
    assert existing.json()["code"] == "invitation_member_exists"
    invitations = f"/api/tenants/{tenant.pk}/invitations/"
    assert post(client, invitations, {"email": "new@example.com"}).status_code == 201
    token = invitation_token()
    repeated = post(client, invitations, {"email": "new@example.com"})
    assert repeated.status_code == 429
    assert repeated["Retry-After"] == "60"
    assert repeated.json() == {
        "code": "invitation_rate_limited",
        "message": "Too many invitations were sent. Wait a minute and try again.",
        "retry_after": 60,
    }

    invitee = User.objects.create_user(username="new", email="new@example.com")
    EmailAddress.objects.create(user=invitee, email=invitee.email, verified=False)
    unverified = post(signed_in(invitee), "/api/invitations/accept/", {"token": token})
    assert unverified.status_code == 403
    assert unverified.json()["code"] == "invitation_email_unverified"
    unknown = post(signed_in(invitee), "/api/invitations/accept/", {"token": "unknown"})
    assert unknown.status_code == 400
    assert unknown.json() == {
        "code": "invitation_unavailable",
        "message": "The invitation is invalid or expired.",
    }


def test_outage_operations_key_each_invalid_value_by_field() -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="PV", dc_capacity_mw=1
    )
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    start = timezone.now() - timedelta(hours=2)
    values: dict[str, Any] = {
        "kind": "other",
        "title": "Loss",
        "notes": "",
        "starts_at": start,
        "ends_at": None,
        "available_dc_mw": None,
        "available_inverter_mw": None,
        "available_export_mw": None,
    }
    for invalid, field in (
        ({"starts_at": start.replace(tzinfo=None)}, "starts_at"),
        ({"ends_at": (start + timedelta(hours=1)).replace(tzinfo=None)}, "ends_at"),
        ({"notes": "x" * 2001}, "notes"),
        ({"available_dc_mw": -1}, "available_dc_mw"),
    ):
        with pytest.raises(DomainError) as refused:
            save_incident(*scope, **(values | invalid))
        assert (refused.value.code, refused.value.field) == ("invalid_request", field)
    incident = save_incident(*scope, **values)
    with pytest.raises(DomainError) as unfinished:
        finish_incident(*scope, incident_id=incident.pk, expected_revision=1)
    assert (unfinished.value.code, unfinished.value.field) == ("invalid_request", "ends_at")
