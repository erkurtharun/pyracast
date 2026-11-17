from datetime import timedelta
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError

from pyracast.accounts.models import Tenant
from pyracast.connections.models import SiteBinding
from pyracast.incidents.models import PlantIncident
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection, EpiasSnapshot
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantSubmission,
    ProductionObservation,
)
from pyracast.regions.models import Region
from pyracast.regions.operations import configure_binding
from tests.test_domain import NOW, System, make_system

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def owners() -> tuple[System, PlantConfiguration, PlantConfiguration]:
    system = make_system()
    plant = Plant.objects.create(region=system.region, name="First")
    tenant = Tenant.objects.create(name="Other tenant")
    region = Region.objects.create(
        tenant=tenant, name="Other location", latitude=41, longitude=30, timezone="UTC"
    )
    other = Plant.objects.create(region=region, name="Other")
    other_binding = configure_binding(
        system.admin,
        region.pk,
        provider_account_id=system.account.pk,
        site_id="other-site",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    first = PlantConfiguration.objects.create(
        region_id=plant.region_id,
        plant=plant,
        binding=system.binding,
        revision=1,
        dc_capacity_mw=1,
        created_by=system.member,
    )
    second = PlantConfiguration.objects.create(
        region_id=other.region_id,
        plant=other,
        binding=other_binding,
        revision=1,
        dc_capacity_mw=1,
        created_by=system.member,
    )
    return system, first, second


def calibration(configuration: PlantConfiguration) -> CalibrationRun:
    return CalibrationRun.objects.create(
        plant=configuration.plant,
        configuration=configuration,
        binding=configuration.binding,
        requested_by=configuration.created_by,
        window_start=NOW.date(),
        window_end=NOW.date(),
        cutoff_at=NOW,
        status="ready",
    )


def test_configuration_rejects_binding_from_another_region(
    owners: tuple[System, PlantConfiguration, PlantConfiguration],
) -> None:
    _, first, second = owners
    with (
        pytest.raises(IntegrityError, match="configuration_binding_region_fk"),
        transaction.atomic(),
    ):
        PlantConfiguration.objects.filter(pk=first.pk).update(binding=second.binding)


@pytest.mark.parametrize("field", ["region", "plant", "binding"])
def test_configuration_creation_requires_one_region_owner(
    owners: tuple[System, PlantConfiguration, PlantConfiguration], field: str
) -> None:
    system, first, second = owners
    values: dict[str, Any] = {
        "plant": first.plant,
        "region": first.region,
        "binding": first.binding,
        "revision": 2,
        "dc_capacity_mw": 1,
        "created_by": system.member,
    }
    values[field] = getattr(second, field)
    with pytest.raises(IntegrityError, match=r"configuration_.*_region_fk"), transaction.atomic():
        PlantConfiguration.objects.create(**values)


@pytest.mark.parametrize("parent", ["plant", "binding"])
def test_configuration_owner_keys_protect_parent_updates(
    owners: tuple[System, PlantConfiguration, PlantConfiguration], parent: str
) -> None:
    _, first, second = owners
    with (  # noqa: PT012 -- deferred FK checks the parent update at commit.
        pytest.raises(IntegrityError, match=f"configuration_{parent}_region_fk"),
        transaction.atomic(),
    ):
        if parent == "plant":
            Plant.objects.filter(pk=first.plant_id).update(region=second.region, deleted_at=NOW)
        else:
            SiteBinding.objects.filter(pk=first.binding_id).update(region=second.region)


@pytest.mark.parametrize(
    "change", ["create", "configuration", "plant", "connection_parent", "configuration_parent"]
)
def test_epias_job_requires_connection_and_configuration_to_share_plant(
    owners: tuple[System, PlantConfiguration, PlantConfiguration], change: str
) -> None:
    system, first, second = owners
    linked = EpiasConnection.objects.create(
        plant=first.plant, provider_plant_id=1, eic="EIC", name="First", connected_by=system.member
    )
    values = {
        "plant": first.plant,
        "connection": linked,
        "configuration": first,
        "connection_revision": 1,
        "provider_plant": {},
        "target_day": NOW.date(),
        "scheduled_for": NOW,
        "next_attempt_at": NOW,
    }
    job = EpiasCollectionJob.objects.create(**values)
    with pytest.raises(IntegrityError, match=r"epias_job_.*_plant_fk"), transaction.atomic():  # noqa: PT012 -- deferred FK checks both child and parent mutations at commit.
        match change:
            case "create":
                EpiasCollectionJob.objects.create(**(values | {"configuration": second}))
            case "configuration":
                EpiasCollectionJob.objects.filter(pk=job.pk).update(configuration=second)
            case "plant":
                EpiasCollectionJob.objects.filter(pk=job.pk).update(plant=second.plant)
            case "connection_parent":
                EpiasConnection.objects.filter(pk=linked.pk).update(plant=second.plant)
            case "configuration_parent":
                PlantConfiguration.objects.filter(pk=first.pk).update(
                    plant=second.plant, region=second.region, binding=second.binding, revision=2
                )


@pytest.mark.parametrize(
    "record", ["current", "observation", "calibration", "activation", "incident"]
)
def test_database_rejects_configuration_from_another_tenant(
    owners: tuple[System, PlantConfiguration, PlantConfiguration], record: str
) -> None:
    system, first, second = owners
    constraint = {
        "current": "plant_current_config_owner_fk",
        "observation": "observation_config_owner_fk",
        "calibration": "calibration_config_owner_fk",
        "activation": "activation_config_owner_fk",
        "incident": "incident_declared_config_owner_fk",
    }[record]
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():  # noqa: PT012 -- commit checks the selected write.
        match record:
            case "current":
                Plant.objects.filter(pk=first.plant_id).update(current_configuration=second)
            case "observation":
                ProductionObservation.objects.create(
                    plant=first.plant,
                    configuration=second,
                    starts_at=NOW,
                    ends_at=NOW + timedelta(hours=1),
                    energy_mwh=1,
                    source="meter",
                    recorded_by=system.member,
                )
            case "calibration":
                run = calibration(second)
                CalibrationRun.objects.filter(pk=run.pk).update(plant=first.plant)
            case "activation":
                CalibrationActivation.objects.create(
                    plant=first.plant, configuration=second, actor=system.member
                )
            case "incident":
                PlantIncident.objects.create(
                    plant=first.plant,
                    declared_configuration=second,
                    kind="grid",
                    title="Unavailable",
                    starts_at=NOW,
                    created_by=system.member,
                    updated_by=system.member,
                )


@pytest.mark.parametrize("record", ["activation", "comparison"])
def test_database_rejects_calibration_from_another_configuration(
    owners: tuple[System, PlantConfiguration, PlantConfiguration], record: str
) -> None:
    system, first, second = owners
    run = calibration(second)
    constraint = {
        "activation": "activation_calibration_config_fk",
        "comparison": "calibration_comparison_config_fk",
    }[record]
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():  # noqa: PT012 -- commit checks the selected write.
        match record:
            case "activation":
                CalibrationActivation.objects.create(
                    plant=first.plant, configuration=first, calibration=run, actor=system.member
                )
            case "comparison":
                activation = CalibrationActivation.objects.create(
                    plant=second.plant, configuration=second, calibration=run, actor=system.member
                )
                candidate = calibration(first)
                CalibrationRun.objects.filter(pk=candidate.pk).update(
                    comparison_activation=activation
                )


def test_database_rechecks_parent_configuration_ownership(
    owners: tuple[System, PlantConfiguration, PlantConfiguration],
) -> None:
    _, first, second = owners
    Plant.objects.filter(pk=first.plant_id).update(current_configuration=first)
    with pytest.raises(IntegrityError, match="plant_current_config_owner_fk"), transaction.atomic():
        PlantConfiguration.objects.filter(pk=first.pk).update(plant=second.plant, revision=2)


@pytest.mark.parametrize(
    "values",
    [
        {"dc_capacity_mw": 0},
        {"dc_capacity_mw": float("nan")},
        {"inverter_capacity_mw": float("inf")},
        {"export_limit_mw": -1},
        {"measurement_basis": "unknown"},
        {"mounting_type": "dual_axis"},
        {"mounting_type": "fixed", "surface_tilt": float("nan")},
        {"mounting_type": "fixed", "surface_azimuth": 360},
        {"mounting_type": "tracking", "surface_tilt": 30},
    ],
)
def test_database_rejects_invalid_customer_declaration(
    owners: tuple[System, PlantConfiguration, PlantConfiguration], values: dict[str, Any]
) -> None:
    system, first, _ = owners
    fields = {"plant": first.plant, "submitted_by": system.member, "dc_capacity_mw": 1} | values
    with pytest.raises(IntegrityError), transaction.atomic():
        PlantSubmission.objects.create(**fields)


def test_epias_archive_keeps_job_configuration_and_connection_lineage(
    owners: tuple[System, PlantConfiguration, PlantConfiguration],
) -> None:
    system, first, second = owners
    linked = EpiasConnection.objects.create(
        plant=first.plant, provider_plant_id=1, eic="EIC", name="First", connected_by=system.member
    )
    other_linked = EpiasConnection.objects.create(
        plant=second.plant,
        provider_plant_id=2,
        eic="OTHER",
        name="Other",
        connected_by=system.member,
    )
    job = EpiasCollectionJob.objects.create(
        plant_id=linked.plant_id,
        connection=linked,
        configuration=first,
        connection_revision=1,
        provider_plant={},
        target_day=NOW.date(),
        scheduled_for=NOW,
        next_attempt_at=NOW,
    )
    fields = dict(
        job=job,
        attempt=1,
        connection=linked,
        configuration=first,
        target_day=NOW.date(),
        collected_at=NOW,
        available_at=NOW,
        raw_payload={},
        readings=[],
        content_sha256="a" * 64,
    )
    for changed in ({"configuration": second}, {"connection": other_linked}):
        with (
            pytest.raises(IntegrityError, match="epias_snapshot_job_lineage_fk"),
            transaction.atomic(),
        ):
            EpiasSnapshot.objects.create(**(fields | changed))
    snapshot = EpiasSnapshot.objects.create(**fields)
    for parent in (job, linked, first):
        with pytest.raises(ProtectedError):
            parent.delete()
    with pytest.raises(IntegrityError, match="epias_snapshot_job_lineage_fk"), transaction.atomic():
        EpiasCollectionJob.objects.filter(pk=job.pk).update(configuration=second)
    assert EpiasSnapshot.objects.get(pk=snapshot.pk).content_sha256 == "a" * 64


def test_lineage_constraints_are_validated_and_deferred() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname, convalidated, condeferrable, condeferred FROM pg_constraint "
            "WHERE conname IN (%s, %s, %s)",
            [
                "plant_current_config_owner_fk",
                "incident_declared_config_owner_fk",
                "epias_snapshot_job_lineage_fk",
            ],
        )
        rows = cursor.fetchall()
    assert len(rows) == 3
    assert all(row[1:] == (True, True, True) for row in rows)


def test_authorized_retention_purge_removes_epias_archive_explicitly(
    owners: tuple[System, PlantConfiguration, PlantConfiguration],
) -> None:
    from pyracast.trash import RETENTION, purge_expired

    system, first, second = owners
    linked = EpiasConnection.objects.create(
        plant=first.plant, provider_plant_id=1, eic="EIC", name="First", connected_by=system.member
    )
    job = EpiasCollectionJob.objects.create(
        plant_id=linked.plant_id,
        connection=linked,
        configuration=first,
        connection_revision=1,
        provider_plant={},
        target_day=NOW.date(),
        scheduled_for=NOW,
        next_attempt_at=NOW,
        status="succeeded",
    )
    EpiasSnapshot.objects.create(
        job=job,
        attempt=1,
        connection=linked,
        configuration=first,
        target_day=NOW.date(),
        collected_at=NOW,
        available_at=NOW,
        raw_payload={},
        readings=[],
        content_sha256="a" * 64,
    )
    Plant.objects.filter(pk=first.plant_id).update(deleted_at=NOW)
    assert purge_expired(NOW + RETENTION) == {"regions": 0, "plants": 1}
    assert not EpiasSnapshot.objects.exists()
    assert not EpiasCollectionJob.objects.exists()
    assert not EpiasConnection.objects.exists()
    assert Plant.objects.filter(pk=second.plant_id).exists()


def test_calibration_preserves_its_configuration_binding(
    owners: tuple[System, PlantConfiguration, PlantConfiguration],
) -> None:
    _, first, second = owners
    run = calibration(first)
    with pytest.raises(IntegrityError, match="calibration_config_binding_fk"), transaction.atomic():
        CalibrationRun.objects.filter(pk=run.pk).update(binding=second.binding)
