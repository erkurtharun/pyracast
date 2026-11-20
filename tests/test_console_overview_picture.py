import pytest
from django.urls import reverse

from pyracast.connections.models import BindingHealth, SiteBinding
from pyracast.connections.operations import rotate_secret
from pyracast.plants.approval import ReviewDecision, decide_review
from pyracast.platform_admin.read_models import overview_context, plant_list_context
from pyracast.regions.operations import request_location_change, submit_region
from tests.support.api import verified_admin
from tests.support.domain import make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_the_overview_counts_suspended_plants_and_plants_needing_attention() -> None:
    system = make_system(approved=True)
    decide_review(system.admin, system.region.pk, ReviewDecision.SUSPEND, reason="Paused.")
    SiteBinding.objects.filter(pk=system.binding.pk).update(health=BindingHealth.ERROR)

    counts = overview_context()["counts"]
    page = verified_admin()[0].get(reverse("admin:index")).content.decode()

    assert (counts["suspended"], counts["needs_attention"]) == (1, 1)
    assert "total" not in counts
    assert "?status=suspended" in page
    assert "Needs attention" in page


def test_the_plant_list_filters_plants_with_a_pending_location_change() -> None:
    system = make_system()
    other = submit_region(
        system.member, system.tenant.pk, name="Other", latitude=41, longitude=30, timezone="UTC"
    )
    decide_review(system.admin, other.pk, ReviewDecision.REJECT, reason="Wrong coordinates.")
    request_location_change(
        system.member,
        other.pk,
        system.tenant.pk,
        expected_revision=other.revision,
        latitude=42,
        longitude=31,
        timezone="UTC",
    )

    listed = plant_list_context("", "", "1", location_change="pending")
    page = verified_admin()[0].get(reverse("admin:index")).content.decode()

    assert [plant.id for plant in listed["plants"]] == [other.pk]
    assert f"{reverse('admin:workspace_plants')}?location_change=pending" in page


def test_the_activity_feed_names_provider_account_events() -> None:
    system = make_system()
    rotate_secret(system.admin, system.account.pk, "test-only-replacement-key")

    activity = overview_context()["recent_activity"]

    rotated = next(row for row in activity if row["action"] == "provider_secret_rotated")
    assert rotated["provider_account__external_account_id"] == "customer"
