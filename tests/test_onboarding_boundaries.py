import pytest
from django.test import Client

from pyracast.connections.operations import rotate_secret
from pyracast.plants.models import PlantConfiguration, PlantEvent, PlantSubmission
from pyracast.plants.operations import EDIT_BLOCK_MESSAGES, PlantEditBlock
from pyracast.regions.models import Region
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("change", ["approval_revoked", "credential_rotated"])
def test_a_plant_without_a_current_approved_binding_refuses_edits_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration_id = plant.current_configuration_id
    assert configuration_id is not None
    previous_configuration = PlantConfiguration.objects.values().get(pk=configuration_id)
    events = list(PlantEvent.objects.filter(plant=plant).values_list("pk", flat=True))
    if change == "approval_revoked":
        Region.objects.filter(pk=system.region.pk).update(status="suspended")
    else:
        rotate_secret(system.admin, system.account.pk, "test-only-rotated-key")

    client = Client()
    client.force_login(system.member)
    response = client.put(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/",
        {"name": "Changed", "dc_capacity_mw": 150, "expected_revision": 1},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "plant_edit_blocked",
        "message": str(
            EDIT_BLOCK_MESSAGES[
                PlantEditBlock.SUSPENDED
                if change == "approval_revoked"
                else PlantEditBlock.CONNECTION_UNAVAILABLE
            ]
        ),
    }
    plant.refresh_from_db()
    assert plant.name == "Plant"
    assert plant.current_configuration_id == previous_configuration["id"]
    assert PlantConfiguration.objects.values().get(pk=configuration_id) == (previous_configuration)
    assert PlantConfiguration.objects.count() == 1
    assert list(PlantEvent.objects.filter(plant=plant).values_list("pk", flat=True)) == events
    assert not PlantSubmission.objects.exists()
