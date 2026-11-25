import html
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.db.models import QuerySet
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.forecasts.validation import validate_binding
from pyracast.plants.approval import approve_region, decide_review
from pyracast.plants.models import Plant, PlantConfiguration, PlantSubmission
from pyracast.plants.operations import create_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.platform_admin.plants import PlantDetailsForm, SiteForm, save_plant_details
from pyracast.regions.models import (
    AuditAction,
    AuditEvent,
    LocationChangeStatus,
    Region,
    RegionLocationChangeRequest,
    RegionStatus,
)
from pyracast.regions.operations import request_location_change
from tests.support.api import verified_admin
from tests.support.domain import NOW, FakeAdapter, System, make_system

pytestmark = pytest.mark.django_db(transaction=True)

ROOT = Path(__file__).resolve().parents[1]


def site_values(**changes: str) -> dict[str, str]:
    return {
        "name": "Console solar",
        "latitude": "41.0082",
        "longitude": "28.9784",
        "timezone": "Europe/Istanbul",
        "dc_capacity_mw": "12.5",
        "inverter_capacity_mw": "10",
        "export_limit_mw": "",
        "measurement_basis": "net_export",
        "mounting_type": "fixed",
        "surface_tilt_degrees": "25",
        "surface_azimuth_degrees": "180",
        **changes,
    }


def test_adding_a_plant_lands_on_its_page_with_the_next_step() -> None:
    tenant = Tenant.objects.create(name="Console customer")
    client, operator = verified_admin()
    url = f"/admin/workspaces/{tenant.pk}/plants/new/"
    page = client.get(url).content.decode()
    assert 'name="timezone" value="Europe/Istanbul"' in page
    assert '<datalist id="timezone-options">' in page
    assert '<option value="Europe/Istanbul">' in page
    for hook in ("data-coordinate-pair", "data-mounting", "data-fixed-mounting"):
        assert hook in page
    assert re.search(r"<p [^>]*data-coordinate-hint hidden>[^<]*41\.0082, 28\.9784", page)
    response = client.post(url, site_values())
    region = Region.objects.get(tenant=tenant)
    assert response.status_code == 302
    assert response["Location"] == f"/admin/plants/{region.pk}/"
    assert (region.name, region.latitude, region.longitude, region.status) == (
        "Console solar",
        41.0082,
        28.9784,
        RegionStatus.PENDING,
    )
    submission = PlantSubmission.objects.get(plant__region=region)
    assert (
        submission.dc_capacity_mw,
        submission.inverter_capacity_mw,
        submission.export_limit_mw,
        submission.mounting_type,
        submission.surface_tilt_degrees,
        submission.surface_azimuth_degrees,
        submission.submitted_by,
    ) == (12.5, 10, None, "fixed", 25, 180, operator)
    html = client.get(response["Location"]).content.decode()
    assert (
        "Plant Console solar created. Next, set up its provider connection, "
        "then validate and approve it."
    ) in html
    assert f'href="/admin/plants/{region.pk}/details/"' in html
    assert f'href="/admin/workspaces/{tenant.pk}/"' in html


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"latitude": "91"}, "latitude", "Ensure this value is less than or equal to 90."),
        ({"dc_capacity_mw": "0"}, "dc_capacity_mw", "Enter a number greater than 0."),
        ({"dc_capacity_mw": ""}, "dc_capacity_mw", "This field is required."),
        (
            {"surface_azimuth_degrees": "360"},
            "surface_azimuth_degrees",
            "Enter a panel azimuth of at least 0° and less than 360°.",
        ),
        (
            {"mounting_type": "tracking"},
            "mounting_type",
            "Enter panel angles only for fixed mounting.",
        ),
        ({"timezone": "Mars/Base"}, "timezone", "Choose a time zone."),
    ],
)
def test_plant_values_are_judged_by_the_site_input_and_the_operation(
    changes: dict[str, str], field: str, message: str
) -> None:
    tenant = Tenant.objects.create(name="Console customer")
    client, _ = verified_admin()
    response = client.post(f"/admin/workspaces/{tenant.pk}/plants/new/", site_values(**changes))
    assert response.status_code == 400
    html = response.content.decode()
    assert message in html
    assert f'href="#id_{field}"' in html
    assert 'value="Console solar"' in html
    assert not Region.objects.exists()


NOT_A_NUMBER = "A valid number is required."

# The single-value table the web's model spec (web/tests/site-forms-model.spec.ts) reads too.
COORDINATE_VALUES: list[dict[str, Any]] = json.loads(
    (ROOT / "web" / "tests" / "support" / "coordinate-values.json").read_text(encoding="utf-8")
)


def console_refusal(axis: str, reason: str) -> str:
    bound = 90 if axis == "latitude" else 180
    return {
        "empty": "This field is required.",
        "not-a-number": NOT_A_NUMBER,
        "above-range": f"Ensure this value is less than or equal to {bound}.",
        "below-range": f"Ensure this value is greater than or equal to -{bound}.",
    }[reason]


ACCEPTED_COORDINATES = [
    (row["axis"], row["text"], row["value"]) for row in COORDINATE_VALUES if "value" in row
]
REFUSED_COORDINATES = [
    (row["axis"], row["text"], console_refusal(row["axis"], row["reason"]))
    for row in COORDINATE_VALUES
    if "reason" in row
]


@pytest.mark.parametrize(("axis", "text", "value"), ACCEPTED_COORDINATES)
def test_a_console_coordinate_with_one_decimal_comma_or_point_is_that_number(
    axis: str, text: str, value: float
) -> None:
    form = SiteForm(data=site_values(**{axis: text}))
    assert form.is_valid(), form.errors
    assert form.site.location()[axis] == value


@pytest.mark.parametrize(("axis", "text", "message"), REFUSED_COORDINATES)
def test_a_console_coordinate_that_is_not_one_plain_number_in_range_is_refused(
    axis: str, text: str, message: str
) -> None:
    form = SiteForm(data=site_values(**{axis: text}))
    assert not form.is_valid()
    assert form.errors[axis] == [message]


def test_coordinates_with_decimal_commas_are_saved_as_numbers_on_both_console_forms() -> None:
    tenant = Tenant.objects.create(name="Console customer")
    client, _ = verified_admin()
    page = client.get(f"/admin/workspaces/{tenant.pk}/plants/new/").content.decode()
    for name in ("latitude", "longitude"):
        field = re.search(rf'<input [^>]*name="{name}"[^>]*>', page)
        assert field is not None
        assert "inputmode" not in field.group(0)
    assert re.search(r'<input [^>]*name="dc_capacity_mw"[^>]*inputmode="decimal"', page)
    created = client.post(
        f"/admin/workspaces/{tenant.pk}/plants/new/",
        site_values(latitude="41,0082", longitude="28,9784"),
    )
    assert created.status_code == 302
    region = Region.objects.get(tenant=tenant)
    assert (region.latitude, region.longitude) == (41.0082, 28.9784)
    corrected = client.post(
        f"/admin/plants/{region.pk}/details/",
        details(
            client,
            region,
            name="Console solar",
            latitude="40,5",
            longitude="-29,25",
            dc_capacity_mw="12.5",
        ),
    )
    assert corrected.status_code == 302
    region.refresh_from_db()
    assert (region.latitude, region.longitude) == (40.5, -29.25)


def test_a_second_plant_at_the_same_location_is_refused_in_the_form() -> None:
    tenant = Tenant.objects.create(name="Console customer")
    client, _ = verified_admin()
    url = f"/admin/workspaces/{tenant.pk}/plants/new/"
    assert client.post(url, site_values()).status_code == 302
    again = client.post(url, site_values(name="Second plant"))
    assert again.status_code == 400
    assert "This workspace already has a plant at this location." in again.content.decode()
    assert Region.objects.count() == 1


def approved_plant() -> tuple[System, Plant]:
    system = make_system(approved=True)
    plant = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        PlantParameters(name="Plant", dc_capacity_mw=130, export_limit_mw=100),
    )
    return system, plant


def details(client: Client, region: Region, **changes: str) -> dict[str, str]:
    page = client.get(f"/admin/plants/{region.pk}/details/").content.decode()
    version = re.search(r'name="expected_version" value="([0-9a-f]+)"', page)
    assert version is not None
    return {
        "name": "Plant",
        "latitude": "40",
        "longitude": "29",
        "timezone": "Europe/Istanbul",
        "dc_capacity_mw": "130",
        "inverter_capacity_mw": "",
        "export_limit_mw": "100",
        "measurement_basis": "net_export",
        "mounting_type": "unknown",
        "surface_tilt_degrees": "",
        "surface_azimuth_degrees": "",
        "reason": "",
        "expected_version": version.group(1),
        **changes,
    }


def test_details_of_an_approved_plant_are_saved_as_a_new_configuration() -> None:
    system, plant = approved_plant()
    client, operator = verified_admin()
    url = f"/admin/plants/{system.region.pk}/details/"
    page = client.get(url).content.decode()
    assert 'name="dc_capacity_mw" value="130"' in page
    assert "Saving a changed location starts a new review" in page
    response = client.post(url, details(client, system.region, dc_capacity_mw="140"))
    assert response.status_code == 302
    assert response["Location"] == f"/admin/plants/{system.region.pk}/"
    configuration = Plant.objects.get(pk=plant.pk).current_configuration
    assert configuration is not None
    assert (configuration.revision, configuration.dc_capacity_mw, configuration.created_by) == (
        2,
        140,
        operator,
    )
    assert Region.objects.get(pk=system.region.pk).status == RegionStatus.APPROVED
    assert "Plant details saved." in client.get(response["Location"]).content.decode()
    unchanged = client.post(url, details(client, system.region, dc_capacity_mw="140"))
    assert unchanged.status_code == 302
    assert "Nothing changed." in client.get(unchanged["Location"]).content.decode()
    assert PlantConfiguration.objects.filter(plant=plant).count() == 2


def test_details_shown_at_full_precision_are_saved_back_without_a_new_revision() -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        PlantParameters(name="Plant", dc_capacity_mw=1.2345678901234567, export_limit_mw=100),
    )
    client, _ = verified_admin()
    url = f"/admin/plants/{system.region.pk}/details/"
    shown = re.search(r'name="dc_capacity_mw" value="([^"]+)"', client.get(url).content.decode())
    assert shown is not None
    assert float(shown.group(1)) == 1.2345678901234567
    unchanged = client.post(url, details(client, system.region, dc_capacity_mw=shown.group(1)))
    assert unchanged.status_code == 302
    assert "Nothing changed." in client.get(unchanged["Location"]).content.decode()
    assert PlantConfiguration.objects.get(plant=plant).revision == 1


def shown_form(page: str) -> dict[str, str]:
    """What a browser posts from the details page's untouched form."""
    fields = {
        name: html.unescape(value)
        for name, value in re.findall(r'<input [^>]*name="([^"]+)"[^>]*value="([^"]*)"', page)
    }
    for name, options in re.findall(r'<select name="([^"]+)"[^>]*>(.*?)</select>', page, re.S):
        selected = re.search(r'<option value="([^"]*)" selected>', options)
        assert selected is not None
        fields[name] = selected.group(1)
    return {**fields, "reason": ""}


# Python writes the smaller of these floats with an exponent, which the form's number rule refuses.
@pytest.mark.parametrize(
    "values",
    [
        {"latitude": "41.0082", "longitude": "-28.9784", "dc_capacity_mw": "130"},
        {"latitude": "0", "longitude": "-29", "surface_tilt_degrees": "0"},
        {"latitude": "0.00005", "longitude": "-0.00001", "dc_capacity_mw": "0.00005"},
        {"latitude": "-0.000001", "longitude": "0.0001", "inverter_capacity_mw": "0.00002"},
        {"export_limit_mw": "0.00003", "surface_tilt_degrees": "0.00004"},
        {"surface_azimuth_degrees": "0.00006"},
    ],
)
def test_details_are_shown_as_plain_numbers_and_saved_back_unchanged(
    values: dict[str, str],
) -> None:
    tenant = Tenant.objects.create(name="Console customer")
    client, _ = verified_admin()
    created = client.post(f"/admin/workspaces/{tenant.pk}/plants/new/", site_values(**values))
    assert created.status_code == 302
    region = Region.objects.get(tenant=tenant)
    stored = (region.latitude, region.longitude, region.revision)
    url = f"/admin/plants/{region.pk}/details/"
    form = shown_form(client.get(url).content.decode())
    assert {name: form[name] for name in values} == values
    unchanged = client.post(url, form)
    assert unchanged.status_code == 302
    assert "Nothing changed." in client.get(unchanged["Location"]).content.decode()
    region.refresh_from_db()
    assert (region.latitude, region.longitude, region.revision) == stored


def test_a_location_correction_returns_the_plant_to_review_with_both_audit_events() -> None:
    system, plant = approved_plant()
    client, operator = verified_admin()
    url = f"/admin/plants/{system.region.pk}/details/"
    response = client.post(
        url,
        details(
            client,
            system.region,
            name="Renamed plant",
            latitude="40.5",
            reason="Surveyed coordinates",
        ),
    )
    assert response.status_code == 302
    region = Region.objects.get(pk=system.region.pk)
    assert (region.latitude, region.revision, region.status, region.name) == (
        40.5,
        system.region.revision + 1,
        RegionStatus.PENDING,
        "Renamed plant",
    )
    change = RegionLocationChangeRequest.objects.get()
    assert (change.status, change.requested_by, change.reviewed_by, change.reason) == (
        LocationChangeStatus.APPROVED,
        operator,
        operator,
        "Surveyed coordinates",
    )
    assert set(
        AuditEvent.objects.filter(region=region, actor=operator).values_list("action", flat=True)
    ) >= {AuditAction.LOCATION_CHANGE_REQUESTED, AuditAction.LOCATION_CHANGE_APPROVED}
    assert PlantConfiguration.objects.get(plant=plant).revision == 1
    html = client.get(response["Location"]).content.decode()
    assert "The plant is back in review with its corrected location." in html

    edited = client.post(
        url,
        details(client, region, name="Renamed plant", latitude="40.5", dc_capacity_mw="150"),
    )
    assert edited.status_code == 302
    submission = PlantSubmission.objects.get(plant=plant)
    assert (submission.dc_capacity_mw, submission.base_configuration_id) == (
        150,
        plant.current_configuration_id,
    )
    assert PlantConfiguration.objects.get(plant=plant).revision == 1


def test_a_waiting_location_request_blocks_a_correction_and_is_linked() -> None:
    system, _ = approved_plant()
    request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=system.region.revision,
        latitude=41,
        longitude=29,
        timezone="UTC",
    )
    client, _ = verified_admin()
    url = f"/admin/plants/{system.region.pk}/details/"
    link = f'href="/admin/plants/{system.region.pk}/#location-title"'
    page = client.get(url).content.decode()
    assert "A location change request is waiting for review." in page
    assert link in page
    refused = client.post(url, details(client, system.region, latitude="40.5"))
    assert refused.status_code == 400
    assert "Approve or reject it before correcting the location." in refused.content.decode()
    assert link in refused.content.decode()
    region = Region.objects.get(pk=system.region.pk)
    assert (region.latitude, region.status) == (40, RegionStatus.APPROVED)
    assert RegionLocationChangeRequest.objects.get().status == LocationChangeStatus.PENDING


def test_a_refused_location_correction_saves_none_of_the_changed_details() -> None:
    system, plant = approved_plant()
    Region.objects.create(
        tenant=system.tenant, name="Neighbour", latitude=40.5, longitude=29, timezone="UTC"
    )
    client, _ = verified_admin()
    url = f"/admin/plants/{system.region.pk}/details/"
    opened = details(
        client, system.region, name="Renamed plant", latitude="40.5", dc_capacity_mw="140"
    )
    refused = client.post(url, opened)
    assert refused.status_code == 400
    assert "This workspace already has a plant at this location." in refused.content.decode()
    version = f'name="expected_version" value="{opened["expected_version"]}"'
    assert version in refused.content.decode()
    region = Region.objects.get(pk=system.region.pk)
    assert (region.name, region.latitude, region.revision, region.status) == (
        system.region.name,
        40,
        system.region.revision,
        RegionStatus.APPROVED,
    )
    assert Plant.objects.get(pk=plant.pk).name == "Plant"
    configuration = PlantConfiguration.objects.get(plant=plant)
    assert (configuration.revision, configuration.dc_capacity_mw) == (1, 130)
    assert not RegionLocationChangeRequest.objects.exists()
    assert not AuditEvent.objects.filter(
        region=region, action=AuditAction.LOCATION_CHANGE_REQUESTED
    ).exists()


def test_a_stale_details_page_asks_for_a_reload_and_a_plant_in_review_updates_its_submission() -> (
    None
):
    system = make_system()
    plant = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        PlantParameters(name="Plant", dc_capacity_mw=5),
    )
    client, _ = verified_admin()
    url = f"/admin/plants/{system.region.pk}/details/"
    stale = client.post(url, {**details(client, system.region), "expected_version": "0" * 64})
    assert stale.status_code == 400
    assert "This plant changed after you opened this page." in stale.content.decode()
    assert f'href="{url}"' in stale.content.decode()
    saved = client.post(
        url,
        details(
            client,
            system.region,
            dc_capacity_mw="7",
            export_limit_mw="",
            mounting_type="fixed",
            surface_tilt_degrees="30",
            surface_azimuth_degrees="170",
        ),
    )
    assert saved.status_code == 302
    submission = PlantSubmission.objects.get(plant=plant)
    assert (
        submission.dc_capacity_mw,
        submission.surface_tilt_degrees,
        submission.surface_azimuth_degrees,
    ) == (
        7,
        30,
        170,
    )
    assert not PlantConfiguration.objects.filter(plant=plant).exists()
    invalid = client.post(url, details(client, system.region, latitude="-91"))
    assert invalid.status_code == 400
    assert "Ensure this value is greater than or equal to -90." in invalid.content.decode()
    empty = Region.objects.create(
        tenant=system.tenant, name="Empty", latitude=1, longitude=1, timezone="UTC"
    )
    assert client.get(f"/admin/plants/{empty.pk}/details/").status_code == 404


def trash_while_the_save_waits(monkeypatch: pytest.MonkeyPatch, region: Region) -> None:
    tenants = Tenant.objects

    class TrashedWhileWaiting:
        @staticmethod
        def select_for_update() -> QuerySet[Tenant]:
            Region.objects.filter(pk=region.pk).update(deleted_at=timezone.now())
            return tenants.select_for_update()

    monkeypatch.setattr(
        "pyracast.platform_admin.plants.Tenant",
        SimpleNamespace(objects=TrashedWhileWaiting, DoesNotExist=Tenant.DoesNotExist),
    )


def test_a_details_save_does_not_find_a_location_moved_to_the_trash_while_it_waited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = approved_plant()
    client, operator = verified_admin()
    form = PlantDetailsForm(details(client, system.region))
    assert form.is_valid(), form.errors
    trash_while_the_save_waits(monkeypatch, system.region)
    with pytest.raises(Region.DoesNotExist):
        save_plant_details(operator, system.region.pk, form)


def test_a_details_page_saved_after_its_location_moved_to_the_trash_answers_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = approved_plant()
    client, _ = verified_admin()
    opened = details(client, system.region, dc_capacity_mw="140")
    trash_while_the_save_waits(monkeypatch, system.region)

    response = client.post(f"/admin/plants/{system.region.pk}/details/", opened)

    assert response.status_code == 404
    configuration = PlantConfiguration.objects.get(plant=plant)
    assert (configuration.revision, configuration.dc_capacity_mw) == (1, 130)


def test_a_details_page_saved_after_its_workspace_was_removed_answers_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = approved_plant()
    client, _ = verified_admin()
    opened = details(client, system.region, dc_capacity_mw="140")
    tenants = Tenant.objects

    class RemovedWhileWaiting:
        @staticmethod
        def select_for_update() -> QuerySet[Tenant]:
            return tenants.select_for_update().exclude(pk=system.tenant.pk)

    monkeypatch.setattr(
        "pyracast.platform_admin.plants.Tenant",
        SimpleNamespace(objects=RemovedWhileWaiting, DoesNotExist=Tenant.DoesNotExist),
    )

    response = client.post(f"/admin/plants/{system.region.pk}/details/", opened)

    assert response.status_code == 404
    configuration = PlantConfiguration.objects.get(plant=plant)
    assert (configuration.revision, configuration.dc_capacity_mw) == (1, 130)


def test_a_details_page_opened_before_an_approval_cannot_save_onto_the_approved_plant() -> None:
    system = make_system()
    plant = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        PlantParameters(name="Plant", dc_capacity_mw=130, export_limit_mw=100),
    )
    client, _ = verified_admin()
    opened = details(client, system.region, dc_capacity_mw="140")
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    approve_region(system.admin, system.region.pk, system.binding.pk, NOW)

    refused = client.post(f"/admin/plants/{system.region.pk}/details/", opened)

    assert refused.status_code == 400
    assert "This plant changed after you opened this page." in refused.content.decode()
    configuration = PlantConfiguration.objects.get(plant=plant)
    assert (configuration.revision, configuration.dc_capacity_mw) == (1, 130)


BLOCKED_FOR_OPERATOR = (
    "Technical details cannot change while the plant is suspended or its data connection is "
    "not current. Approve the plant again or validate its connection first."
)


def test_a_blocked_details_save_tells_the_operator_what_to_do_and_saves_nothing() -> None:
    system, plant = approved_plant()
    decide_review(system.admin, system.region.pk, "suspend", reason="Paused by support.")
    client, _ = verified_admin()
    url = f"/admin/plants/{system.region.pk}/details/"
    response = client.post(
        url, details(client, system.region, dc_capacity_mw="140", latitude="40.5")
    )
    assert response.status_code == 400
    page = response.content.decode()
    assert BLOCKED_FOR_OPERATOR in page
    assert "Contact Pyracast support" not in page
    region = Region.objects.get(pk=system.region.pk)
    assert (region.status, region.latitude) == (RegionStatus.SUSPENDED, 40)
    assert Plant.objects.get(pk=plant.pk).current_configuration_id == plant.current_configuration_id


def test_a_location_correction_of_a_suspended_plant_returns_it_to_review() -> None:
    system, _plant = approved_plant()
    decide_review(system.admin, system.region.pk, "suspend", reason="Paused by support.")
    client, _ = verified_admin()
    url = f"/admin/plants/{system.region.pk}/details/"
    response = client.post(url, details(client, system.region, latitude="40.5"))
    assert response.status_code == 302
    region = Region.objects.get(pk=system.region.pk)
    assert (region.status, region.latitude, region.approved_binding_id) == (
        RegionStatus.PENDING,
        40.5,
        None,
    )
