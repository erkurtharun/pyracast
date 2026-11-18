from dataclasses import replace
from datetime import date, timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import F

from pyracast.accounts.models import Membership
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastSnapshot,
    HistoricalReferenceImport,
    ReferenceCapacityNormalization,
)
from pyracast.metering.models import EpiasConnection, EpiasSnapshot
from pyracast.plants.models import Plant, PlantConfiguration, ProductionObservation
from pyracast.regions.models import Region
from tests.support.capacity_normalization import normalize, scaled_system
from tests.support.domain import NOW, System, make_system
from tests.support.plant_operations import make_plant
from tests.support.plant_training import actual_snapshot_for
from tests.support.provisional_measurements import acquisition
from tests.support.trash_operations import sibling

pytestmark = pytest.mark.django_db(transaction=True)

IMMUTABLE = "are immutable"


def _reference_import(system: System) -> HistoricalReferenceImport:
    return HistoricalReferenceImport.objects.create(
        binding=system.binding,
        sha256="a" * 64,
        source_name="fixture",
        source_settings={},
        imported_by=system.admin,
        imported_at=NOW,
        interval_count=1,
    )


def _observation(configuration: PlantConfiguration, **values: Any) -> ProductionObservation:
    start = NOW - timedelta(days=2)
    return ProductionObservation.objects.create(
        **{
            "plant_id": configuration.plant_id,
            "configuration": configuration,
            "starts_at": start,
            "ends_at": start + timedelta(hours=1),
            "energy_mwh": 1,
            "source": "manual",
            "recorded_by": _member(configuration),
        }
        | values
    )


def _configuration(monkeypatch: pytest.MonkeyPatch) -> PlantConfiguration:
    _, plant = make_plant(monkeypatch)
    return PlantConfiguration.objects.get(plant=plant)


def _member(configuration: PlantConfiguration) -> User:
    return Membership.objects.get(tenant_id=configuration.plant.region.tenant_id).user


def _sibling_configuration(configuration: PlantConfiguration) -> PlantConfiguration:
    source = configuration.binding
    region, binding = sibling(
        System(
            _member(configuration),
            _member(configuration),
            configuration.plant.region.tenant,
            configuration.plant.region,
            source.provider_account,
            source,
        )
    )
    plant = Plant.objects.create(region=region, name="Other plant")
    return PlantConfiguration.objects.create(
        **{
            field.name: getattr(configuration, field.name)
            for field in PlantConfiguration._meta.concrete_fields
            if field.name not in {"id", "plant", "region", "binding", "created_at"}
        },
        plant=plant,
        region=region,
        binding=binding,
    )


@pytest.mark.parametrize(
    "values", [{"source_name": "changed"}, {"interval_count": 2}, {"sha256": "b" * 64}]
)
def test_a_stored_historical_reference_import_is_never_rewritten(values: dict[str, Any]) -> None:
    record = _reference_import(make_system())
    rows = HistoricalReferenceImport.objects.filter(pk=record.pk)

    with pytest.raises(IntegrityError, match=IMMUTABLE), transaction.atomic():
        rows.update(**values)

    rows.delete()
    assert not HistoricalReferenceImport.objects.exists()


@pytest.mark.parametrize(
    "values",
    [{"readings": []}, {"complete": True}, {"available_at": NOW}, {"provider_plant": {}}],
)
def test_a_stored_epias_snapshot_is_never_rewritten(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, Any]
) -> None:
    _, plant = make_plant(monkeypatch)
    configuration = PlantConfiguration.objects.get(plant=plant)
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        eic="EIC",
        name="Plant",
        connected_by=_member(configuration),
    )
    snapshot = acquisition((configuration, connection))
    rows = EpiasSnapshot.objects.filter(pk=snapshot.pk)

    with pytest.raises(IntegrityError, match=IMMUTABLE), transaction.atomic():
        rows.update(**values)

    rows.delete()
    assert not EpiasSnapshot.objects.exists()


@pytest.mark.parametrize(
    "values",
    [
        {"energy_mwh": 2},
        {"quality": "curtailed"},
        {"recorded_at": NOW},
        {"source": "corrected"},
        {"is_current": False, "energy_mwh": 2},
    ],
)
def test_a_production_revision_changes_only_from_current_to_superseded(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, Any]
) -> None:
    observation = _observation(_configuration(monkeypatch))
    rows = ProductionObservation.objects.filter(pk=observation.pk)

    with pytest.raises(IntegrityError, match=IMMUTABLE), transaction.atomic():
        rows.update(**values)

    rows.update(is_current=False)
    with pytest.raises(IntegrityError, match=IMMUTABLE), transaction.atomic():
        rows.update(is_current=True)
    rows.delete()
    assert not ProductionObservation.objects.exists()


@pytest.mark.parametrize("shift", ["plant", "interval"])
def test_a_production_revision_supersedes_the_same_plant_and_interval(
    monkeypatch: pytest.MonkeyPatch, shift: str
) -> None:
    configuration = _configuration(monkeypatch)
    previous = _observation(configuration)
    ProductionObservation.objects.filter(pk=previous.pk).update(is_current=False)
    other = configuration if shift == "interval" else _sibling_configuration(configuration)
    starts = previous.starts_at + (timedelta(hours=1) if shift == "interval" else timedelta())

    with (
        pytest.raises(IntegrityError, match="same plant and interval"),
        transaction.atomic(),
    ):
        ProductionObservation.objects.create(
            plant_id=other.plant_id,
            configuration=other,
            starts_at=starts,
            ends_at=starts + timedelta(hours=1),
            energy_mwh=2,
            source="manual",
            recorded_by=previous.recorded_by,
            supersedes=previous,
        )

    revision = _observation(configuration, supersedes=previous, energy_mwh=2)
    assert revision.supersedes_id == previous.pk


def test_a_capacity_normalization_stores_the_factor_of_its_capacities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    actual_snapshot_for(system, date(2026, 11, 7))
    normalize(system, target)
    rows = ReferenceCapacityNormalization.objects.all()

    with (
        pytest.raises(IntegrityError, match="referencecapacitynormalization_factor_chk"),
        transaction.atomic(),
    ):
        rows.update(factor=F("factor") * 2)

    rows.update(target_capacity_kw=F("target_capacity_kw") * 2, factor=F("factor") * 2)
    assert rows.get().factor == 2000


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"endpoint": "historical_import"}, "collectionjob_reference_import_chk"),
        (
            {"endpoint": "historical_import", "status": "pending"},
            "collectionjob_reference_import_chk",
        ),
    ],
)
def test_only_reference_imports_use_the_historical_import_endpoint(
    values: dict[str, Any], constraint: str
) -> None:
    system = make_system()
    job = CollectionJob.objects.create(
        binding=system.binding, scheduled_slot=NOW, target_date=NOW.date(), available_at=NOW
    )

    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        CollectionJob.objects.filter(pk=job.pk).update(**values)


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"endpoint": "forecasts"}, "collectionjob_reference_import_chk"),
        ({"status": "pending"}, "collectionjob_import_status_chk"),
        ({"status": "stale"}, "collectionjob_import_status_chk"),
    ],
)
def test_an_imported_reference_day_is_stored_as_a_succeeded_import(
    values: dict[str, Any], constraint: str
) -> None:
    system = make_system()
    job = CollectionJob.objects.create(
        binding=system.binding,
        reference_import=_reference_import(system),
        endpoint="historical_import",
        scheduled_slot=NOW,
        target_date=NOW.date(),
        available_at=NOW,
        status="succeeded",
    )

    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        CollectionJob.objects.filter(pk=job.pk).update(**values)


def test_two_source_revisions_of_one_slot_normalize_onto_one_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    second = SiteBinding.objects.get(pk=system.binding.pk)
    second.pk = None
    second.region_revision = target.region_revision + 1
    second.save()
    SiteBinding.objects.filter(pk=target.pk).update(region_revision=F("region_revision") + 2)
    Region.objects.filter(pk=system.region.pk).update(revision=F("revision") + 2)
    day = date(2026, 11, 7)
    first_source = actual_snapshot_for(system, day)
    second_source = actual_snapshot_for(replace(system, binding=second), day)
    CollectionJob.objects.filter(pk=second_source.job_id).update(
        scheduled_slot=first_source.job.scheduled_slot
    )
    copies = normalize(system, target)
    copies += normalize(replace(system, binding=second), target)

    assert len(copies) == 2
    assert {
        snapshot.job.scheduled_slot
        for snapshot in ForecastSnapshot.objects.filter(pk__in=copies).select_related("job")
    } == {first_source.job.scheduled_slot}
