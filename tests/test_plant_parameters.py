from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import Client

from pyracast.errors import DomainError, ErrorCode
from pyracast.forecasts.validation import validate_binding
from pyracast.plants.approval import approve_region, decide_review, resubmit_region
from pyracast.plants.models import Plant, PlantConfiguration, PlantEvent, PlantSubmission
from pyracast.plants.operations import create_plant, revise_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.plants.serializers import PlantInput
from pyracast.plants.sites import submit_site
from pyracast.platform_admin.read_models import recent_activity_context
from pyracast.regions.models import AuditEvent, Region
from pyracast.regions.operations import configure_binding, request_location_change
from tests.support.domain import NOW, FakeAdapter, System, make_system

pytestmark = pytest.mark.django_db(transaction=True)

VALUES: dict[str, Any] = {"name": "Plant", "dc_capacity_mw": 130, "export_limit_mw": 100}
SITE: dict[str, Any] = {
    **VALUES,
    "name": "New plant",
    "latitude": 41.5,
    "longitude": 30.5,
    "timezone": "Europe/Istanbul",
}
INF, NAN = float("inf"), float("nan")


def declared_plant(*, approved: bool) -> tuple[System, Plant]:
    system = make_system(approved=approved)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, PlantParameters(**VALUES)
    )
    assert (plant.current_configuration_id is not None) is approved
    return system, plant


def session(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def plant_url(system: System, plant: Plant) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/"


def listed(client: Client, system: System) -> dict[str, Any]:
    response = client.get(f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/")
    assert response.status_code == 200, response.content
    row: dict[str, Any] = response.json()["results"][0]
    return row


def save(client: Client, system: System, plant: Plant, **body: Any) -> Any:
    return client.put(plant_url(system, plant), {**VALUES, **body}, content_type="application/json")


def declarations(plant: Plant) -> int:
    return (
        PlantConfiguration.objects.filter(plant=plant).count()
        + PlantEvent.objects.filter(plant=plant, action="submission_saved").count()
    )


@pytest.mark.parametrize("approved", [False, True], ids=["pending", "approved"])
def test_a_save_from_an_older_revision_is_refused_with_the_current_plant(approved: bool) -> None:
    system, plant = declared_plant(approved=approved)
    first, second = session(system.member), session(system.member)
    seen = listed(first, system)["parameters_revision"]
    assert listed(second, system)["parameters_revision"] == seen

    saved = save(first, system, plant, dc_capacity_mw=140, expected_revision=seen)
    assert saved.status_code == 200, saved.content
    assert saved.json()["parameters_revision"] == seen + 1
    written = declarations(plant)

    refused = save(second, system, plant, export_limit_mw=90, expected_revision=seen)
    assert refused.status_code == 409, refused.content
    body = refused.json()
    assert body["code"] == "plant_parameters_changed"
    assert body["current"]["parameters_revision"] == seen + 1
    assert body["current"]["parameters"]["dc_capacity_mw"] == 140
    assert body["current"]["parameters"]["export_limit_mw"] == 100
    assert declarations(plant) == written

    retried = save(
        second, system, plant, dc_capacity_mw=140, export_limit_mw=90, expected_revision=seen + 1
    )
    assert retried.status_code == 200, retried.content
    assert retried.json()["parameters_revision"] == seen + 2
    assert retried.json()["parameters"]["export_limit_mw"] == 90


def test_two_sessions_saving_from_one_revision_at_once_let_exactly_one_win() -> None:
    system, plant = declared_plant(approved=True)
    seen = listed(session(system.member), system)["parameters_revision"]
    barrier = Barrier(2)

    def submit(capacity: float) -> int:
        close_old_connections()
        try:
            client = session(User.objects.get(pk=system.member.pk))
            barrier.wait(timeout=10)
            response = save(client, system, plant, dc_capacity_mw=capacity, expected_revision=seen)
            return int(response.status_code)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(submit, (140, 150)))
    assert codes == [200, 409]
    assert PlantConfiguration.objects.filter(plant=plant).count() == 2
    plant.refresh_from_db()
    assert plant.parameters_revision == seen + 1


def test_an_unchanged_save_from_an_older_revision_keeps_the_current_revision() -> None:
    system, plant = declared_plant(approved=True)
    client = session(system.member)
    seen = listed(client, system)["parameters_revision"]
    assert (
        save(client, system, plant, dc_capacity_mw=140, expected_revision=seen).status_code == 200
    )
    written = declarations(plant)

    replayed = save(client, system, plant, dc_capacity_mw=140, expected_revision=seen)

    assert replayed.status_code == 200, replayed.content
    assert replayed.json()["parameters_revision"] == seen + 1
    assert declarations(plant) == written


CHANGES: dict[str, dict[str, Any]] = {
    "name": {"name": "Renamed"},
    "dc_capacity_mw": {"dc_capacity_mw": 131},
    "inverter_capacity_mw": {"inverter_capacity_mw": 120},
    "export_limit_mw": {"export_limit_mw": None},
    "measurement_basis": {"measurement_basis": "ac_generation"},
    "mounting_type": {"mounting_type": "tracking"},
    "surface_tilt_degrees": {"mounting_type": "fixed", "surface_tilt_degrees": 20},
    "surface_azimuth_degrees": {"mounting_type": "fixed", "surface_azimuth_degrees": 180},
}


@pytest.mark.parametrize("approved", [False, True], ids=["pending", "approved"])
@pytest.mark.parametrize("field", CHANGES)
def test_a_change_of_any_declared_value_advances_the_revision(approved: bool, field: str) -> None:
    system, plant = declared_plant(approved=approved)
    client = session(system.member)
    seen = listed(client, system)["parameters_revision"]

    response = save(client, system, plant, **CHANGES[field], expected_revision=seen)

    assert response.status_code == 200, response.content
    assert response.json()["parameters_revision"] == seen + 1
    assert save(client, system, plant, expected_revision=seen).status_code == 409


def test_a_plant_rejected_after_returning_to_review_keeps_an_edit_for_its_next_approval() -> None:
    system, plant = declared_plant(approved=True)
    replacement = configure_binding(
        system.admin,
        system.region.pk,
        provider_account_id=system.account.pk,
        site_id="replacement",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    decide_review(system.admin, system.region.pk, "reject", reason="Check the export limit.")

    revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        PlantParameters(**{**VALUES, "export_limit_mw": 90}),
    )

    client = session(system.member)
    edited = listed(client, system)
    assert edited["parameters"]["export_limit_mw"] == 90
    assert edited["current_configuration"]["export_limit_mw"] == 100
    region = Region.objects.get(pk=system.region.pk)
    resubmit_region(system.member, system.tenant.pk, region.pk, expected_revision=region.revision)
    validate_binding(system.admin, replacement.pk, FakeAdapter(), NOW)
    approve_region(system.admin, region.pk, replacement.pk, NOW)
    plant.refresh_from_db()
    configuration = plant.current_configuration
    assert configuration is not None
    assert (configuration.binding_id, configuration.export_limit_mw) == (replacement.pk, 90)
    assert listed(client, system)["parameters"]["export_limit_mw"] == 90


FIELD_ERRORS: list[tuple[dict[str, Any], str]] = [
    ({"name": " "}, "name"),
    ({"name": "x" * 201}, "name"),
    ({"dc_capacity_mw": 0}, "dc_capacity_mw"),
    ({"dc_capacity_mw": True}, "dc_capacity_mw"),
    ({"dc_capacity_mw": NAN}, "dc_capacity_mw"),
    ({"inverter_capacity_mw": INF}, "inverter_capacity_mw"),
    ({"export_limit_mw": -1}, "export_limit_mw"),
    ({"measurement_basis": "dc"}, "measurement_basis"),
    ({"mounting_type": "dual_axis"}, "mounting_type"),
    ({"mounting_type": "unknown", "surface_tilt_degrees": 0}, "mounting_type"),
    ({"mounting_type": "fixed", "surface_tilt_degrees": 91}, "surface_tilt_degrees"),
    ({"mounting_type": "fixed", "surface_azimuth_degrees": 360}, "surface_azimuth_degrees"),
]


@pytest.mark.parametrize(("arguments", "field"), FIELD_ERRORS)
def test_a_refused_plant_value_names_its_field(arguments: dict[str, Any], field: str) -> None:
    system = make_system()
    with pytest.raises(DomainError) as error:
        create_plant(
            system.member,
            system.tenant.pk,
            system.region.pk,
            PlantParameters(**{**VALUES, **arguments}),
        )
    assert (error.value.code, error.value.field) == ("invalid_request", field)
    assert not Plant.objects.exists()


def test_the_plant_command_reports_why_a_value_was_refused() -> None:
    system = make_system(approved=True)
    with pytest.raises(CommandError, match="Capacities must be finite and positive"):
        call_command(
            "configure_plant",
            username="member",
            region_id=system.region.pk,
            name="Plant",
            dc_capacity_mw=0.0,
        )


DECLARED = {
    "dc_capacity_mw": 1,
    "inverter_capacity_mw": None,
    "export_limit_mw": None,
    "measurement_basis": "net_export",
    "mounting_type": "fixed",
    "surface_tilt_degrees": None,
    "surface_azimuth_degrees": None,
}
PROBES: list[dict[str, Any]] = [
    *({"dc_capacity_mw": value} for value in (-1, 0, 1e-9, 1e300, INF, NAN)),
    *({"inverter_capacity_mw": value} for value in (None, 0, 1e-9, INF)),
    *({"export_limit_mw": value} for value in (None, -1, 1e-9, NAN)),
    *({"measurement_basis": value} for value in ("net_export", "ac_generation", "dc")),
    *({"mounting_type": value} for value in ("unknown", "fixed", "tracking", "dual_axis")),
    *({"surface_tilt_degrees": value} for value in (-1e-9, 0, 90, 90.000001, NAN)),
    *({"surface_azimuth_degrees": value} for value in (-1e-9, 0, 359.999999, 360, INF)),
    {"mounting_type": "tracking", "surface_tilt_degrees": 10},
    {"mounting_type": "unknown", "surface_azimuth_degrees": 10},
]


class _UndoneError(Exception):
    pass


def _stored(create: Any) -> bool:
    try:
        with transaction.atomic():
            create()
            raise _UndoneError
    except _UndoneError:
        return True
    except IntegrityError:
        return False


def _accepted_by_the_operation(system: System, plant: Plant, values: dict[str, Any]) -> bool:
    try:
        with transaction.atomic():
            revise_plant(
                system.member,
                system.tenant.pk,
                system.region.pk,
                plant.pk,
                PlantParameters(name="P", **values),
            )
            raise _UndoneError
    except _UndoneError:
        return True
    except ValidationError:
        return False


@pytest.mark.parametrize("probe", PROBES, ids=repr)
def test_stored_checks_the_input_and_the_operation_accept_the_same_plant_values(
    probe: dict[str, Any],
) -> None:
    system, plant = declared_plant(approved=True)
    values = {**DECLARED, **probe}
    accepted = {
        "submission": _stored(
            lambda: PlantSubmission.objects.create(
                plant=plant, submitted_by=system.member, **values
            )
        ),
        "configuration": _stored(
            lambda: PlantConfiguration.objects.create(
                plant=plant,
                region=system.region,
                binding=system.binding,
                revision=99,
                created_by=system.member,
                **values,
            )
        ),
        "input": PlantInput(data={"name": "P", **values}).is_valid(),
        "operation": _accepted_by_the_operation(system, plant, values),
    }
    assert len(set(accepted.values())) == 1, accepted


@pytest.mark.parametrize(
    "zone", ["Europe\\Istanbul", "posix/Europe/Istanbul", "right/UTC", "Mars/Base"]
)
def test_a_zone_outside_the_available_zones_is_refused_naming_the_field(zone: str) -> None:
    system = make_system(approved=True)
    response = session(system.member).post(
        f"/api/tenants/{system.tenant.pk}/sites/",
        {**SITE, "timezone": zone},
        content_type="application/json",
        headers={"Accept-Language": "tr"},
    )
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "invalid_request"
    assert response.json()["fields"] == {"timezone": "Geçerli bir IANA saat dilimi girin."}
    with pytest.raises(DomainError) as error:
        request_location_change(
            system.member,
            system.region.pk,
            system.tenant.pk,
            expected_revision=system.region.revision,
            latitude=41,
            longitude=30,
            timezone=zone,
        )
    assert error.value.field == "timezone"
    assert Region.objects.count() == 1


def test_a_site_submission_records_one_creation_event_the_console_lists() -> None:
    system = make_system()
    response = session(system.member).post(
        f"/api/tenants/{system.tenant.pk}/sites/", SITE, content_type="application/json"
    )
    assert response.status_code == 201, response.content
    region_id = response.json()["id"]
    events = AuditEvent.objects.filter(region_id=region_id)
    assert [(event.action, event.actor_id, event.revision) for event in events] == [
        ("region_created", system.member.pk, 1)
    ]
    assert recent_activity_context(region_id)[0]["action_label"] == "Plant submitted"


def test_a_site_submission_failing_after_its_location_records_no_creation_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system()
    events = AuditEvent.objects.count()

    def refuse(*_arguments: object, **_keywords: object) -> None:
        raise DomainError(ErrorCode.LOCATION_ALREADY_HAS_PLANT)

    monkeypatch.setattr("pyracast.plants.sites.create_plant", refuse)
    with pytest.raises(DomainError):
        submit_site(
            system.member,
            system.tenant.pk,
            PlantParameters(**VALUES),
            latitude=41.5,
            longitude=30.5,
            timezone="Europe/Istanbul",
        )
    assert AuditEvent.objects.count() == events
    assert Region.objects.count() == 1
