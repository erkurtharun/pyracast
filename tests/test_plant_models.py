from datetime import timedelta
from typing import Any

import pytest
from django.db import IntegrityError, transaction

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import CollectionJob
from pyracast.plants.models import (
    CalibrationRun,
    Plant,
    PlantConfiguration,
    ProductionObservation,
)
from pyracast.regions.operations import submit_region
from tests.test_domain import NOW, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_observation_overlap_is_scoped_to_plant_and_not_configuration() -> None:
    system = make_system()
    plant = Plant.objects.create(region=system.region, name="One")
    other_region = submit_region(
        system.member,
        system.tenant.pk,
        name="Other",
        latitude=41,
        longitude=29,
        timezone="Europe/Istanbul",
    )
    other_binding = SiteBinding.objects.get(pk=system.binding.pk)
    other_binding.pk = None
    other_binding.region = other_region
    other_binding.save()
    other = Plant.objects.create(region=other_region, name="Two")
    config = PlantConfiguration.objects.create(
        region_id=plant.region_id,
        plant=plant,
        binding=system.binding,
        revision=1,
        dc_capacity_mw=1,
        created_by=system.member,
    )
    revised = PlantConfiguration.objects.create(
        region_id=plant.region_id,
        plant=plant,
        binding=system.binding,
        revision=2,
        dc_capacity_mw=1,
        created_by=system.member,
    )
    other_config = PlantConfiguration.objects.create(
        region_id=other.region_id,
        plant=other,
        binding=other_binding,
        revision=1,
        dc_capacity_mw=1,
        created_by=system.member,
    )
    ProductionObservation.objects.create(
        plant=plant,
        configuration=config,
        starts_at=NOW,
        ends_at=NOW + timedelta(hours=1),
        energy_mwh=1,
        source="meter",
        recorded_by=system.member,
    )
    ProductionObservation.objects.create(
        plant=other,
        configuration=other_config,
        starts_at=NOW,
        ends_at=NOW + timedelta(hours=1),
        energy_mwh=1,
        source="meter",
        recorded_by=system.member,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        ProductionObservation.objects.create(
            plant=plant,
            configuration=revised,
            starts_at=NOW + timedelta(minutes=30),
            ends_at=NOW + timedelta(minutes=60),
            energy_mwh=1,
            source="meter",
            recorded_by=system.member,
        )
    ProductionObservation.objects.create(
        plant=plant,
        configuration=revised,
        starts_at=NOW + timedelta(hours=1),
        ends_at=NOW + timedelta(hours=1, minutes=30),
        energy_mwh=1,
        source="meter",
        recorded_by=system.member,
    )


def test_database_rejects_invalid_values_and_duplicate_baseline_forecast() -> None:
    system = make_system(approved=True)
    plant = Plant.objects.create(region=system.region, name="One")
    for capacity in (0.0, float("inf"), float("nan")):
        with pytest.raises(IntegrityError), transaction.atomic():
            PlantConfiguration.objects.create(
                region_id=plant.region_id,
                plant=plant,
                binding=system.binding,
                revision=1,
                dc_capacity_mw=capacity,
                created_by=system.member,
            )
    config = PlantConfiguration.objects.create(
        region_id=plant.region_id,
        plant=plant,
        binding=system.binding,
        revision=1,
        dc_capacity_mw=1,
        created_by=system.member,
    )
    for energy in (-1.0, float("inf"), float("nan")):
        with pytest.raises(IntegrityError), transaction.atomic():
            ProductionObservation.objects.create(
                plant=plant,
                configuration=config,
                starts_at=NOW,
                ends_at=NOW + timedelta(minutes=30),
                energy_mwh=energy,
                source="meter",
                recorded_by=system.member,
            )
    with pytest.raises(IntegrityError), transaction.atomic():
        ProductionObservation.objects.create(
            plant=plant,
            configuration=config,
            starts_at=NOW,
            ends_at=NOW + timedelta(minutes=15),
            energy_mwh=1,
            source="meter",
            recorded_by=system.member,
        )
    job = CollectionJob.objects.get()
    run = dict(
        plant=plant,
        configuration=config,
        binding=system.binding,
        requested_by=system.member,
        window_start=job.target_date,
        window_end=job.target_date,
        cutoff_at=NOW,
    )
    CalibrationRun.objects.create(**run)
    with pytest.raises(IntegrityError), transaction.atomic():
        CalibrationRun.objects.create(**run)


def test_database_enforces_mounting_and_angle_contract() -> None:
    system = make_system(approved=True)
    plant = Plant.objects.create(region=system.region, name="Geometry")
    invalid: list[dict[str, Any]] = [
        {"mounting_type": "dual_axis"},
        {"mounting_type": "unknown", "surface_tilt": 0},
        {"mounting_type": "tracking", "surface_azimuth": 180},
        {"mounting_type": "fixed", "surface_tilt": -1},
        {"mounting_type": "fixed", "surface_tilt": 91},
        {"mounting_type": "fixed", "surface_tilt": float("nan")},
        {"mounting_type": "fixed", "surface_tilt": float("inf")},
        {"mounting_type": "fixed", "surface_azimuth": -1},
        {"mounting_type": "fixed", "surface_azimuth": 360},
        {"mounting_type": "fixed", "surface_azimuth": float("nan")},
        {"mounting_type": "fixed", "surface_azimuth": float("inf")},
    ]
    for values in invalid:
        with pytest.raises(IntegrityError), transaction.atomic():
            PlantConfiguration.objects.create(
                region_id=plant.region_id,
                plant=plant,
                binding=system.binding,
                revision=1,
                dc_capacity_mw=1,
                created_by=system.member,
                **values,
            )
    configuration = PlantConfiguration.objects.create(
        region_id=plant.region_id,
        plant=plant,
        binding=system.binding,
        revision=1,
        dc_capacity_mw=1,
        created_by=system.member,
        mounting_type="fixed",
        surface_tilt=0,
        surface_azimuth=0,
    )
    assert (configuration.surface_tilt, configuration.surface_azimuth) == (0, 0)
