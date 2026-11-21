from typing import Any

import pytest
from django.contrib.auth.models import User
from django.db.models import F
from django.test import Client

from pyracast.accounts.models import Membership, WorkspaceRole
from pyracast.connections.models import SiteBinding
from pyracast.plants.approval import decide_review
from pyracast.plants.models import Plant, PlantSubmission
from pyracast.plants.operations import create_plant, revise_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.plants.sites import submit_site
from pyracast.regions.models import Region
from pyracast.regions.operations import submit_region
from tests.support.budgets import assert_scale_invariant
from tests.support.domain import System, make_system
from tests.support.errors import raises_code
from tests.support.plant_operations import make_plant
from tests.support.trash_operations import sibling, sibling_plant

pytestmark = pytest.mark.django_db(transaction=True)


def _sites(system: System) -> dict[int, dict[str, Any]]:
    client = Client()
    client.force_login(system.member)
    response = client.get(f"/api/tenants/{system.tenant.pk}/sites/")
    assert response.status_code == 200, response.content
    return {row["id"]: row for row in response.json()["results"]}


def _states(system: System, region: Region) -> tuple[str | None, str | None]:
    row = _sites(system)[region.pk]
    return row["attention"], row["edit_blocked_reason"]


def _pending_site(system: System, latitude: float) -> Region:
    return submit_site(
        system.member,
        system.tenant.pk,
        PlantParameters(name=f"Pending {latitude}", dc_capacity_mw=3),
        latitude=latitude,
        longitude=31,
        timezone="Europe/Istanbul",
    )


def test_an_approved_plant_with_a_current_connection_needs_no_attention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    assert _states(system, system.region) == (None, None)


def test_an_approved_plant_whose_connection_stopped_is_marked_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    system.account.credential_revision += 1
    system.account.save(update_fields=("credential_revision",))
    with raises_code("plant_edit_blocked"):
        revise_plant(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            PlantParameters(name="Plant", dc_capacity_mw=140, export_limit_mw=100),
        )
    assert _states(system, system.region) == (
        "connection_unavailable",
        "connection_unavailable",
    )


def test_a_suspended_plant_is_marked_and_its_technical_details_are_refused_with_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    decide_review(system.admin, system.region.pk, "suspend", reason="Paused by support.")
    client = Client()
    client.force_login(system.member)
    response = client.put(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/",
        {
            "name": "Plant",
            "dc_capacity_mw": 140,
            "export_limit_mw": 100,
            "expected_revision": plant.parameters_revision,
        },
        content_type="application/json",
    )
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "plant_edit_blocked"
    assert "suspended" in response.json()["message"]
    assert Plant.objects.get(pk=plant.pk).current_configuration_id == plant.current_configuration_id
    assert _states(system, system.region) == ("suspended", "suspended")


def test_a_rejected_plant_needs_attention_and_stays_editable() -> None:
    system = make_system()
    region = _pending_site(system, 37)
    decide_review(system.admin, region.pk, "reject", reason="Wrong capacity.")
    assert _states(system, region) == ("review_rejected", None)


def test_a_pending_plant_needs_no_attention() -> None:
    system = make_system()
    region = _pending_site(system, 37)
    assert _states(system, region) == (None, None)


def test_a_location_without_a_plant_needs_its_setup_completed() -> None:
    system = make_system()
    assert _states(system, system.region) == ("setup_incomplete", None)


def test_a_suspended_location_without_a_plant_refuses_setup_and_writes_no_plant() -> None:
    system = make_system(approved=True)
    decide_review(system.admin, system.region.pk, "suspend", reason="Paused by support.")
    with raises_code("plant_edit_blocked"):
        create_plant(
            system.member,
            system.tenant.pk,
            system.region.pk,
            PlantParameters(name="Site", dc_capacity_mw=5),
        )
    assert not Plant.objects.filter(region=system.region).exists()
    assert _states(system, system.region) == ("suspended", "suspended")


def test_a_rejected_location_without_a_plant_records_the_setup_for_its_resubmission() -> None:
    system = make_system()
    decide_review(system.admin, system.region.pk, "reject", reason="Add the plant.")
    plant = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        PlantParameters(name="Site", dc_capacity_mw=5),
    )
    assert PlantSubmission.objects.get(plant=plant).dc_capacity_mw == 5


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_the_site_list_reads_a_fixed_number_of_statements_at_one_and_ten_times_the_plants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    added = 0

    def seed(count: int) -> None:
        nonlocal added
        for _ in range(count):
            added += 1
            broken = sibling_plant(system)
            SiteBinding.objects.filter(region=broken.region).update(
                validated_credential_revision=F("validated_credential_revision") + 1
            )
            suspended, _binding = sibling(system)
            create_plant(
                system.member,
                system.tenant.pk,
                suspended.pk,
                PlantParameters(name=f"Suspended {added}", dc_capacity_mw=2),
            )
            decide_review(system.admin, suspended.pk, "suspend", reason="Paused.")
            rejected = _pending_site(system, 10 + added)
            decide_review(system.admin, rejected.pk, "reject", reason="Wrong.")
            submit_region(
                system.member,
                system.tenant.pk,
                name=f"Location {added}",
                latitude=-10 - added,
                longitude=30,
                timezone="Europe/Istanbul",
            )

    client = Client()
    client.force_login(system.member)
    url = f"/api/tenants/{system.tenant.pk}/sites/"
    assert_scale_invariant(seed, lambda: client.get(url), small=1, large=10)
    rows = client.get(url).json()["results"]
    assert len(rows) == 41
    attention = [row["attention"] for row in rows]
    assert attention.count(None) == 1
    for reason in ("connection_unavailable", "suspended", "review_rejected", "setup_incomplete"):
        assert attention.count(reason) == 10


def test_a_location_already_in_the_portfolio_names_the_plant_that_holds_it() -> None:
    system = make_system()
    region = _pending_site(system, 37)
    client = Client()
    client.force_login(system.member)
    response = client.post(
        f"/api/tenants/{system.tenant.pk}/sites/",
        {
            "name": "Second",
            "latitude": 37,
            "longitude": 31,
            "timezone": "Europe/Istanbul",
            "dc_capacity_mw": 4,
        },
        content_type="application/json",
    )
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "location_already_exists"
    assert response.json()["region_id"] == region.pk


def _region_in(system: System, state: str, *, with_plant: bool) -> Region:
    if with_plant:
        create_plant(
            system.member,
            system.tenant.pk,
            system.region.pk,
            PlantParameters(name="Plant", dc_capacity_mw=5),
        )
    if state == "connection_stopped":
        system.account.credential_revision += 1
        system.account.save(update_fields=("credential_revision",))
    elif state in {"suspended", "rejected"}:
        decision = "suspend" if state == "suspended" else "reject"
        decide_review(system.admin, system.region.pk, decision, reason="Decided.")
    return system.region


@pytest.mark.parametrize(
    ("state", "with_plant", "expected"),
    [
        ("pending", True, (None, None)),
        ("pending", False, ("setup_incomplete", None)),
        ("approved", True, (None, None)),
        ("approved", False, ("setup_incomplete", None)),
        ("connection_stopped", True, ("connection_unavailable", "connection_unavailable")),
        ("connection_stopped", False, ("connection_unavailable", "connection_unavailable")),
        ("suspended", True, ("suspended", "suspended")),
        ("suspended", False, ("suspended", "suspended")),
        ("rejected", True, ("review_rejected", None)),
        ("rejected", False, ("setup_incomplete", None)),
    ],
)
def test_a_block_is_named_before_an_incomplete_setup_and_that_before_a_rejection(
    state: str, with_plant: bool, expected: tuple[str | None, str | None]
) -> None:
    system = make_system(approved=state in {"approved", "connection_stopped", "suspended"})
    region = _region_in(system, state, with_plant=with_plant)
    assert _states(system, region) == expected


def test_a_save_that_is_both_blocked_and_stale_is_refused_for_the_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    decide_review(system.admin, system.region.pk, "suspend", reason="Paused by support.")
    with raises_code("plant_edit_blocked"):
        revise_plant(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            PlantParameters(name="Plant", dc_capacity_mw=140, export_limit_mw=100),
            expected_revision=plant.parameters_revision + 1,
        )


@pytest.mark.parametrize(
    ("role", "status", "code"),
    [
        (WorkspaceRole.VIEWER, 403, "workspace_admin_required"),
        (None, 403, "membership_required"),
    ],
)
def test_a_viewer_or_an_outsider_writing_to_a_blocked_plant_learns_only_the_refused_access(
    monkeypatch: pytest.MonkeyPatch, role: WorkspaceRole | None, status: int, code: str
) -> None:
    system, plant = make_plant(monkeypatch)
    decide_review(system.admin, system.region.pk, "suspend", reason="Paused by support.")
    user = User.objects.create(username="writer")
    if role is not None:
        Membership.objects.create(user=user, tenant=system.tenant, role=role)
    client = Client()
    client.force_login(user)
    response = client.put(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/",
        {
            "name": "Plant",
            "dc_capacity_mw": 140,
            "export_limit_mw": 100,
            "expected_revision": plant.parameters_revision,
        },
        content_type="application/json",
    )
    assert (response.status_code, response.json()["code"]) == (status, code)
    assert "suspended" not in response.json()["message"]
    assert Plant.objects.get(pk=plant.pk).parameters_revision == plant.parameters_revision
