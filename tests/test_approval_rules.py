import re
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.validation import validate_binding
from pyracast.plants import approval
from pyracast.plants.approval import ReviewDecision, approve_region, decide_review
from pyracast.plants.models import Plant, PlantConfiguration
from pyracast.plants.operations import create_plant, revise_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.platform_admin import plant_review
from pyracast.regions.models import AuditAction, AuditEvent, Region, RegionStatus
from pyracast.regions.operations import configure_binding, request_location_change
from tests.support.admin_setup import admin_system, form_fields, submitted_plant
from tests.support.api import verified_admin
from tests.support.domain import NOW, FakeAdapter, System, make_system
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)

REASON = "The declared capacity does not match the grid licence."
APPROVED = "Plant approved. The first saved forecast is being prepared."
ALREADY_APPROVED = "This plant is already approved."
DECLARATION_CHANGED = (
    "The declared plant details changed after this page opened. "
    "The details shown now are the current ones; review them before approving."
)
STATUS_CHANGED = (
    "The review status changed after this page opened. Reload the plant and review it again."
)
DECISION_UNAVAILABLE = "This review decision is unavailable for the current review status."
LAST_VALIDATION_FAILED = (
    "The last validation failed. Validate the connection again before approval."
)
REJECTED = "Plant rejected."
ALREADY_REJECTED = "This plant is already rejected."
REVIEW_NOT_APPLIED = "The review could not be applied. Reload and review the current plant."
LOCATION_APPROVED = "Location change approved."
LOCATION_ALREADY_APPROVED = "This location change is already approved."


def plant_url(region: Region) -> str:
    return reverse("admin:workspace_plant", args=(region.pk,))


def decision_url(region: Region) -> str:
    return reverse("admin:workspace_decision", args=(region.pk,))


def validated(system: System) -> System:
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), timezone.now())
    return system


def actions_of(response: Any) -> dict[str, str]:
    """The hidden inputs of the form that holds the validate and approve buttons."""
    return form_fields(response.content, "validate")


def page_actions(client: Client, region: Region) -> dict[str, str]:
    return actions_of(client.get(plant_url(region)))


def review_decision(region: Region, action: str) -> dict[str, object]:
    region.refresh_from_db()
    return {
        "expected_revision": region.revision,
        "expected_status": region.status,
        "action": action,
        **({"decision_reason": REASON} if action in {"reject", "suspend"} else {}),
    }


def notices(client: Client, url: str, data: dict[str, object]) -> tuple[int, list[str]]:
    response = client.post(url, data, follow=True)
    status = response.redirect_chain[0][1] if response.redirect_chain else response.status_code
    return status, [str(message) for message in response.context["messages"]]


def status_of(region: Region) -> str:
    region.refresh_from_db()
    return region.status


def test_approval_is_refused_when_the_declaration_changed_after_the_page_opened() -> None:
    system = validated(admin_system())
    plant = submitted_plant(system)
    client, _ = verified_admin()
    seen = page_actions(client, system.region)
    revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        PlantParameters(
            name=plant.name, dc_capacity_mw=9.9, inverter_capacity_mw=4, export_limit_mw=3.8
        ),
    )

    response = client.post(plant_url(system.region), {**seen, "action": "approve"})

    assert response.status_code == 400
    assert DECLARATION_CHANGED in response.content.decode()
    assert status_of(system.region) == RegionStatus.PENDING
    assert not PlantConfiguration.objects.filter(plant=plant).exists()


def test_approval_from_a_page_opened_before_a_rejection_is_refused() -> None:
    system = validated(admin_system())
    submitted_plant(system)
    client, _ = verified_admin()
    seen = page_actions(client, system.region)
    client.post(decision_url(system.region), review_decision(system.region, "reject"))

    response = client.post(plant_url(system.region), {**seen, "action": "approve"})

    assert response.status_code == 400
    assert STATUS_CHANGED in response.content.decode()
    assert status_of(system.region) == RegionStatus.REJECTED


def test_a_rejected_plant_cannot_be_approved() -> None:
    system = validated(admin_system())
    submitted_plant(system)
    client, _ = verified_admin()
    client.post(decision_url(system.region), review_decision(system.region, "reject"))

    response = client.post(
        plant_url(system.region), {**page_actions(client, system.region), "action": "approve"}
    )

    assert response.status_code == 400
    assert DECISION_UNAVAILABLE in response.content.decode()
    assert status_of(system.region) == RegionStatus.REJECTED


def test_a_suspended_plant_is_reinstated_by_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    system, _ = make_plant(monkeypatch)
    client, _ = verified_admin()
    client.post(decision_url(system.region), review_decision(system.region, "suspend"))

    response = client.post(
        plant_url(system.region), {**page_actions(client, system.region), "action": "approve"}
    )

    assert response.status_code == 302
    assert status_of(system.region) == RegionStatus.APPROVED


def test_approval_after_a_rejected_key_is_refused_until_validation_succeeds() -> None:
    system = validated(admin_system())
    submitted_plant(system)

    def reject_key() -> None:
        raise ProviderError("provider_request_failed", retryable=False, status_code=401)

    with pytest.raises(ProviderError):
        validate_binding(system.admin, system.binding.pk, FakeAdapter(reject_key), timezone.now())
    client, _ = verified_admin()

    response = client.post(
        plant_url(system.region), {**page_actions(client, system.region), "action": "approve"}
    )

    assert response.status_code == 400
    assert LAST_VALIDATION_FAILED in response.content.decode()
    assert status_of(system.region) == RegionStatus.PENDING


def test_a_double_approval_reports_the_approval_once() -> None:
    system = validated(admin_system())
    submitted_plant(system)
    client, _ = verified_admin()
    seen = {**page_actions(client, system.region), "action": "approve"}

    url = plant_url(system.region)

    assert notices(client, url, seen) == (302, [APPROVED])
    assert notices(client, url, seen) == (302, [ALREADY_APPROVED])
    assert AuditEvent.objects.filter(action=AuditAction.REGION_APPROVED).count() == 1


def test_a_double_rejection_reports_the_rejection_once() -> None:
    system = admin_system()
    client, _ = verified_admin()
    seen = review_decision(system.region, "reject")

    url = decision_url(system.region)

    assert notices(client, url, seen) == (302, [REJECTED])
    assert notices(client, url, seen) == (302, [ALREADY_REJECTED])
    assert AuditEvent.objects.filter(action=AuditAction.REGION_REJECTED).count() == 1


def test_a_location_decision_without_a_location_change_is_refused() -> None:
    system = admin_system()
    client, _ = verified_admin()

    outcome = notices(
        client, decision_url(system.region), review_decision(system.region, "approve_location")
    )

    assert outcome == (302, [REVIEW_NOT_APPLIED])


def test_a_double_location_approval_reports_the_approval_once() -> None:
    system = admin_system()
    change = request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=system.region.revision,
        latitude=41,
        longitude=30,
        timezone="UTC",
    )
    client, _ = verified_admin()
    seen = {**review_decision(system.region, "approve_location"), "location_change_id": change.pk}

    url = decision_url(system.region)

    assert notices(client, url, seen) == (302, [LOCATION_APPROVED])
    assert notices(client, url, seen) == (302, [LOCATION_ALREADY_APPROVED])
    assert AuditEvent.objects.filter(action=AuditAction.LOCATION_CHANGE_APPROVED).count() == 1


def test_a_location_decision_is_refused_after_the_review_status_changed() -> None:
    system = admin_system()
    change = request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=system.region.revision,
        latitude=41,
        longitude=30,
        timezone="UTC",
    )
    client, _ = verified_admin()
    seen = {**review_decision(system.region, "reject_location"), "location_change_id": change.pk}
    decide_review(system.admin, system.region.pk, ReviewDecision.REJECT, reason=REASON)

    assert notices(client, decision_url(system.region), seen) == (302, [STATUS_CHANGED])
    change.refresh_from_db()
    assert change.status == "pending"


def customer_edit(system: System, plant: Plant, **changes: Any) -> None:
    values: dict[str, Any] = {"name": "East array", "dc_capacity_mw": 100, **changes}
    revise_plant(
        system.member, system.tenant.pk, system.region.pk, plant.pk, PlantParameters(**values)
    )


def edited_while_approved_then_back_in_review() -> tuple[System, Plant, SiteBinding]:
    """Submitted at 100 MW and approved, edited to 150 MW while approved, then back in review
    with a replacement binding; the first submission row still holds 100 MW."""
    system = make_system()
    plant = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        PlantParameters(name="East array", dc_capacity_mw=100),
    )
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    approve_region(system.admin, system.region.pk, system.binding.pk, NOW)
    customer_edit(system, plant, dc_capacity_mw=150)
    replacement = configure_binding(
        system.admin,
        system.region.pk,
        provider_account_id=system.account.pk,
        site_id="replacement",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    validate_binding(system.admin, replacement.pk, FakeAdapter(), NOW)
    assert status_of(system.region) == RegionStatus.PENDING
    return system, plant, replacement


def activated(plant: Plant) -> tuple[int, int, float]:
    plant.refresh_from_db()
    configuration = plant.current_configuration
    assert configuration is not None
    return configuration.binding_id, configuration.revision, configuration.dc_capacity_mw


def test_the_review_page_shows_and_approval_activates_the_values_edited_while_approved() -> None:
    system, plant, replacement = edited_while_approved_then_back_in_review()
    client, _ = verified_admin()

    page = client.get(plant_url(system.region)).content.decode()

    assert "<dd>150.0 MW</dd>" in page
    assert "<dd>100.0 MW</dd>" not in page
    approval = {**page_actions(client, system.region), "action": "approve"}
    assert notices(client, plant_url(system.region), approval) == (302, [APPROVED])
    assert activated(plant) == (replacement.pk, 3, 150)


@pytest.mark.parametrize(
    "edit",
    [{"dc_capacity_mw": 100}, {"dc_capacity_mw": 150, "name": "West array"}],
    ids=["back_to_the_value_of_the_earlier_submission", "rename"],
)
def test_an_edit_after_the_review_page_opened_refuses_the_approval(edit: dict[str, Any]) -> None:
    system, plant, _ = edited_while_approved_then_back_in_review()
    client, _ = verified_admin()
    seen = page_actions(client, system.region)

    customer_edit(system, plant, **edit)
    response = client.post(plant_url(system.region), {**seen, "action": "approve"})

    assert response.status_code == 400
    assert DECLARATION_CHANGED in response.content.decode()
    assert status_of(system.region) == RegionStatus.PENDING
    assert activated(plant) == (system.binding.pk, 2, 150)


def shown_capacity(response: Any) -> float:
    shown = re.findall(r"<dd>([0-9.]+) MW</dd>", response.content.decode())
    assert len(shown) == 1, shown
    return float(shown[0])


def test_the_values_a_review_page_shows_are_the_values_its_approval_activates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, replacement = edited_while_approved_then_back_in_review()
    client, _ = verified_admin()
    read = approval.declarations
    edits = iter([175])

    def edited_after_the_read(region_id: int) -> Any:
        rows = read(region_id)
        for capacity in edits:
            customer_edit(system, plant, dc_capacity_mw=capacity)
        return rows

    with monkeypatch.context() as patched:
        patched.setattr(approval, "declarations", edited_after_the_read)
        patched.setattr(plant_review, "declarations", edited_after_the_read)
        page = client.get(plant_url(system.region))

    shown = shown_capacity(page)
    customer_edit(system, plant, dc_capacity_mw=shown)
    approved = {**actions_of(page), "action": "approve"}
    assert notices(client, plant_url(system.region), approved) == (302, [APPROVED])
    assert activated(plant) == (replacement.pk, 3, shown)


def refused_after_an_edit_to_175() -> tuple[System, Plant, SiteBinding, Client, Any]:
    system, plant, replacement = edited_while_approved_then_back_in_review()
    client, _ = verified_admin()
    seen = page_actions(client, system.region)
    customer_edit(system, plant, dc_capacity_mw=175)
    refused = client.post(plant_url(system.region), {**seen, "action": "approve"})
    assert refused.status_code == 400
    assert DECLARATION_CHANGED in refused.content.decode()
    assert shown_capacity(refused) == 175
    return system, plant, replacement, client, refused


def test_a_page_refused_for_changed_values_approves_the_values_it_shows() -> None:
    system, plant, replacement, client, refused = refused_after_an_edit_to_175()

    again = {**actions_of(refused), "action": "approve"}

    assert notices(client, plant_url(system.region), again) == (302, [APPROVED])
    assert activated(plant) == (replacement.pk, 3, 175)


def test_a_page_refused_for_changed_values_carries_the_form_a_fresh_page_carries() -> None:
    system, _, _, client, refused = refused_after_an_edit_to_175()

    assert actions_of(refused) == page_actions(client, system.region)


def test_a_page_refused_for_changed_values_refuses_when_they_change_again() -> None:
    system, plant, _, client, refused = refused_after_an_edit_to_175()
    customer_edit(system, plant, dc_capacity_mw=180)

    again = client.post(plant_url(system.region), {**actions_of(refused), "action": "approve"})

    assert again.status_code == 400
    assert DECLARATION_CHANGED in again.content.decode()
    assert shown_capacity(again) == 180
    assert activated(plant) == (system.binding.pk, 2, 150)


def test_values_edited_away_and_back_after_the_review_page_opened_are_approved() -> None:
    system, plant, replacement = edited_while_approved_then_back_in_review()
    client, _ = verified_admin()
    seen = {**page_actions(client, system.region), "action": "approve"}

    customer_edit(system, plant, dc_capacity_mw=120)
    customer_edit(system, plant, dc_capacity_mw=150)

    assert notices(client, plant_url(system.region), seen) == (302, [APPROVED])
    assert activated(plant) == (replacement.pk, 3, 150)


def trash(region: Region) -> None:
    Region.objects.filter(pk=region.pk).update(deleted_at=timezone.now())


def test_a_review_page_whose_location_moved_to_the_trash_before_it_rendered_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _, _ = edited_while_approved_then_back_in_review()
    client, _ = verified_admin()
    read = plant_review.declarations

    def trashed_after_the_page_loaded(region_id: int) -> Any:
        trash(system.region)
        return read(region_id)

    monkeypatch.setattr(plant_review, "declarations", trashed_after_the_page_loaded)

    assert client.get(plant_url(system.region)).status_code == 404


def test_an_approval_whose_location_moved_to_the_trash_while_it_waited_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _ = edited_while_approved_then_back_in_review()
    client, _ = verified_admin()
    seen = {**page_actions(client, system.region), "action": "approve"}
    approve = plant_review.approve_region

    def trashed_while_waiting(*arguments: Any, **keywords: Any) -> Any:
        trash(system.region)
        return approve(*arguments, **keywords)

    monkeypatch.setattr(plant_review, "approve_region", trashed_while_waiting)

    assert client.post(plant_url(system.region), seen).status_code == 404
    assert activated(plant) == (system.binding.pk, 2, 150)


def test_the_review_page_names_the_plant_from_the_read_its_values_and_hash_come_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _ = edited_while_approved_then_back_in_review()
    client, _ = verified_admin()
    read = plant_review.declarations

    def renamed_after_the_read(region_id: int) -> Any:
        rows = read(region_id)
        customer_edit(system, plant, name="Renamed array", dc_capacity_mw=150)
        return rows

    monkeypatch.setattr(plant_review, "declarations", renamed_after_the_read)
    page = client.get(plant_url(system.region)).content.decode()

    assert "<h1>East array</h1>" in page
    assert "<title>East array " in page
    assert "Renamed array" not in page
