import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from pyracast.connections.contracts import ProviderError
from pyracast.forecasts.validation import validate_binding
from pyracast.plants.approval import ReviewDecision, decide_review
from pyracast.plants.models import PlantConfiguration
from pyracast.plants.operations import revise_plant
from pyracast.regions.models import AuditAction, AuditEvent, Region, RegionStatus
from pyracast.regions.operations import request_location_change
from tests.support.admin_setup import admin_system, submitted_plant
from tests.support.api import verified_admin
from tests.support.domain import FakeAdapter, System
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)

REASON = "The declared capacity does not match the grid licence."
APPROVED = "Plant approved. The first saved forecast is being prepared."
ALREADY_APPROVED = "This plant is already approved."
DECLARATION_CHANGED = (
    "The declared plant details changed after this page opened. Review them again before approving."
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


def page_actions(client: Client, region: Region) -> dict[str, object]:
    form = client.get(plant_url(region)).context["action_form"]
    values = {name: form[name].value() for name in form.fields}
    return {name: value for name, value in values.items() if value is not None}


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
        name=plant.name,
        dc_capacity_mw=9.9,
        inverter_capacity_mw=4,
        export_limit_mw=3.8,
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
