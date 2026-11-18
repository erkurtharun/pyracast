from typing import Any

import pytest
from django.core.exceptions import PermissionDenied

from pyracast.errors import DomainError
from pyracast.regions.models import (
    AuditAction,
    AuditEvent,
    LocationChangeStatus,
    Region,
    RegionLocationChangeRequest,
    RegionStatus,
)
from pyracast.regions.operations import correct_location, request_location_change
from tests.support.domain import make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_location_correction_requests_and_approves_in_one_step() -> None:
    system = make_system(approved=True)
    correction = correct_location(
        system.admin,
        system.region.pk,
        expected_revision=system.region.revision,
        latitude=40.1234567,
        longitude=29.5,
        timezone="Europe/Istanbul",
        reason="Surveyed coordinates",
    )
    region = Region.objects.get(pk=system.region.pk)
    assert (region.latitude, region.longitude, region.revision, region.status) == (
        40.123457,
        29.5,
        system.region.revision + 1,
        RegionStatus.PENDING,
    )
    assert region.approved_binding_id is None
    assert (correction.status, correction.requested_by, correction.reviewed_by) == (
        LocationChangeStatus.APPROVED,
        system.admin,
        system.admin,
    )
    assert correction.reason == "Surveyed coordinates"
    assert list(
        AuditEvent.objects.filter(region=region, actor=system.admin)
        .order_by("pk")
        .values_list("action", flat=True)
    )[-2:] == [AuditAction.LOCATION_CHANGE_REQUESTED, AuditAction.LOCATION_CHANGE_APPROVED]


def test_location_correction_refusals_change_nothing() -> None:
    system = make_system()
    revision = system.region.revision
    with pytest.raises(PermissionDenied):
        correct_location(
            system.member,
            system.region.pk,
            expected_revision=revision,
            latitude=41,
            longitude=29,
            timezone="UTC",
        )
    refusals: dict[str, dict[str, Any]] = {
        "location_unchanged": {"latitude": 40, "longitude": 29, "timezone": "Europe/Istanbul"},
        "region_revision_changed": {
            "expected_revision": revision + 1,
            "latitude": 41,
            "longitude": 29,
            "timezone": "UTC",
        },
    }
    for code, values in refusals.items():
        with pytest.raises(DomainError) as refusal:
            correct_location(
                system.admin, system.region.pk, **{"expected_revision": revision, **values}
            )
        assert refusal.value.code == code
    request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=revision,
        latitude=41,
        longitude=29,
        timezone="UTC",
    )
    with pytest.raises(DomainError) as refusal:
        correct_location(
            system.admin,
            system.region.pk,
            expected_revision=revision,
            latitude=41,
            longitude=29,
            timezone="UTC",
        )
    assert refusal.value.code == "location_change_already_pending"
    region = Region.objects.get(pk=system.region.pk)
    assert (region.latitude, region.revision) == (40, revision)
    assert RegionLocationChangeRequest.objects.get().status == LocationChangeStatus.PENDING
