from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier
from typing import Any, cast

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, close_old_connections, connection, transaction

from pyracast.accounts.models import Membership, Tenant
from pyracast.forecasts.models import ForecastSnapshot, HistoricalReferenceImport
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_archive import forecast_cutoff
from pyracast.plants.forecast_reconstruction import reconstruct_forecasts
from pyracast.plants.forecasting import archived_forecast
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    ForecastIssuance,
    ForecastReconstruction,
    Plant,
    PlantConfiguration,
    PlantEvent,
)
from pyracast.plants.operations import activate_calibration, reset_calibration
from pyracast.regions.models import Region
from pyracast.regions.operations import submit_region
from pyracast.trash import RETENTION, purge_expired, trash_plant
from tests.test_domain import NOW, System
from tests.test_plant_operations import make_plant, publish_snapshot, ready_run, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = NOW.date() - timedelta(days=1)


def prepared(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[System, Plant, CalibrationRun, ForecastSnapshot]:
    system, plant = make_plant(monkeypatch)
    run = ready_run(system, plant)
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)
    source = raw_source(system, DAY)
    return system, plant, run, source


def raw_source(system: System, day: date, *, newer: bool = False) -> ForecastSnapshot:
    start, end = local_day_bounds(day, system.binding.timezone)
    source = snapshot_for(system, day, collected_at=start - timedelta(minutes=30 if newer else 60))
    source.coverage_start, source.coverage_end = start, end
    source.available_at = source.collected_at
    source.save()
    source.job.status = "succeeded"
    source.job.save()
    return source


def outlook(system: System, plant: Plant, **kwargs: Any) -> dict[str, Any]:
    return plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=kwargs.pop("resolution_minutes", 30),
        source=kwargs.pop("source", "archived_forecast"),
        **kwargs,
    )


def test_preview_write_idempotency_and_honest_frozen_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, run, source = prepared(monkeypatch)
    assert outlook(system, plant)["result"] is None
    preview = reconstruct_forecasts([plant.pk], DAY, DAY, NOW)
    assert preview[0]["status"] == "preview"
    assert not ForecastReconstruction.objects.exists()
    applied = reconstruct_forecasts([plant.pk], DAY, DAY, NOW, apply=True)
    assert applied[0]["status"] == "created"
    assert (
        reconstruct_forecasts([plant.pk], DAY, DAY, NOW, apply=True)[0]["status"]
        == "already_exists"
    )
    saved = ForecastReconstruction.objects.get()
    assert saved.calibration_id == run.pk
    assert saved.snapshot_id == source.pk
    assert saved.source_cutoff_at == forecast_cutoff(DAY, system.binding)
    assert not ForecastIssuance.objects.exists()
    assert PlantEvent.objects.filter(action="forecast_reconstructed").get().details == {
        "plant_id": plant.pk,
        "date": DAY.isoformat(),
        "snapshot_id": source.pk,
        "configuration_id": plant.current_configuration_id,
        "calibration_id": run.pk,
        "reconstruction_id": saved.pk,
        "reconstructed_at": NOW.isoformat(),
    }
    for resolution in (30, 60):
        response = outlook(system, plant, resolution_minutes=resolution)
        result = response["result"]
        assert result["energy_mwh"] == pytest.approx(624)
        assert response["issuance_id"] is None
        assert response["calibration_id"] == run.pk
        assert response["source_kind"] == "forecast"
        assert response["outage_scenario"] is None
        assert response["production_diagnostic"] is None
        assert "reconstructed_forecast" in result["quality_flags"]
        assert "archived_forecast" not in result["quality_flags"]
        assert result["provenance"]["reconstructed_at"] == NOW.isoformat()
        assert (
            result["provenance"]["available_at"] == cast(datetime, source.available_at).isoformat()
        )
        assert result["provenance"]["factor"] == 0.5
    source.normalized_intervals[0]["power_kw"] = 99
    source.save()
    reset_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk)
    assert outlook(system, plant)["result"] == saved.payloads["30"]


def test_reconstruction_never_leaks_into_as_of_core_archive_or_automatic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _, _ = prepared(monkeypatch)
    reconstruct_forecasts([plant.pk], DAY, DAY, NOW, apply=True)
    assert archived_forecast(plant, DAY, 30, NOW)["result"] is None
    assert outlook(system, plant, as_of=NOW)["result"] is None
    assert outlook(system, plant, source="auto")["result"] is None
    ForecastReconstruction.objects.all().delete()
    reconstruct_forecasts([plant.pk], DAY, DAY, NOW + timedelta(seconds=1), apply=True)
    assert outlook(system, plant)["result"] is None


def test_original_issuance_wins_and_writer_does_not_replace_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _, source = prepared(monkeypatch)
    reconstruct_forecasts([plant.pk], DAY, DAY, NOW, apply=True)
    publish_snapshot(source)
    result = outlook(system, plant)
    assert result["issuance_id"] == ForecastIssuance.objects.get().pk
    assert result["result"]["energy_mwh"] == pytest.approx(1248)
    assert "reconstructed_forecast" not in result["result"]["quality_flags"]
    assert (
        reconstruct_forecasts([plant.pk], DAY, DAY, NOW, apply=True)[0]["status"]
        == "issuance_exists"
    )
    assert ForecastReconstruction.objects.count() == 1


@pytest.mark.parametrize(
    "invalid",
    [
        "availability",
        "issue",
        "kind",
        "mixed",
        "mixed_tags",
        "failed",
        "configuration",
        "import",
        "horizon",
        "endpoint",
    ],
)
def test_ineligible_sources_cannot_be_reconstructed(
    monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    system, plant, _, source = prepared(monkeypatch)
    if invalid == "availability":
        source.available_at = forecast_cutoff(DAY, system.binding)
    elif invalid == "issue":
        source.issued_at = source.collected_at + timedelta(seconds=1)
    elif invalid == "kind":
        source.source_kind = "estimated_actuals"
    elif invalid == "mixed":
        source.normalized_intervals[0]["source_kind"] = "estimated_actuals"
    elif invalid == "mixed_tags":
        source.normalized_intervals[0]["source_kinds"] = ["forecast", "estimated_actuals"]
    elif invalid == "endpoint":
        source.job.endpoint = "estimated_actuals"
        source.job.save()
    elif invalid == "failed":
        source.job.status = "failed"
        source.job.save()
    elif invalid == "configuration":
        PlantConfiguration.objects.filter(plant=plant).update(created_at=NOW)
    elif invalid == "import":
        source.job.reference_import = HistoricalReferenceImport.objects.create(
            binding=system.binding,
            sha256="a" * 64,
            source_name="history.csv",
            source_settings={},
            imported_by=system.admin,
            imported_at=source.collected_at,
            interval_count=48,
        )
        source.job.save()
    else:
        source.collected_at -= timedelta(days=3)
        source.available_at = source.collected_at
    source.save()
    result = reconstruct_forecasts([plant.pk], DAY, DAY, NOW, apply=True)
    assert result[0]["status"] == "source_unavailable"
    assert not ForecastReconstruction.objects.exists()


def test_latest_partial_is_not_replaced_by_older_complete_and_night_gaps_are_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _, _ = prepared(monkeypatch)
    source = raw_source(system, DAY, newer=True)
    source.normalized_intervals[24]["power_kw"] = None
    source.save()
    assert (
        reconstruct_forecasts([plant.pk], DAY, DAY, NOW, apply=True)[0]["status"]
        == "incomplete_source"
    )
    source.normalized_intervals[24]["power_kw"] = 0.4
    source.normalized_intervals = source.normalized_intervals[10:40]
    source.save()
    assert reconstruct_forecasts([plant.pk], DAY, DAY, NOW, apply=True)[0]["status"] == "created"
    payload = outlook(system, plant)["result"]
    assert payload["complete"] is False
    assert payload["energy_mwh"] is None
    assert payload["daylight"]["complete"] is True
    assert payload["intervals"][0]["energy_mwh"] is None


@pytest.mark.parametrize("reason", ["missing", "reset", "identity", "no_configuration"])
def test_requires_current_usable_calibration(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    system, plant, run, _ = prepared(monkeypatch)
    if reason in {"missing", "reset"}:
        reset_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk)
    elif reason == "identity":
        run.production_snapshot_ids = [999999]
        run.save()
    else:
        Plant.objects.filter(pk=plant.pk).update(current_configuration=None)
    with pytest.raises(ValueError, match="usable activated calibration"):
        reconstruct_forecasts([plant.pk], DAY, DAY, NOW, apply=True)
    assert not ForecastReconstruction.objects.exists()


@pytest.mark.parametrize("invalid", ["naive", "reversed", "future", "plant", "empty"])
def test_invalid_request_cannot_write(monkeypatch: pytest.MonkeyPatch, invalid: str) -> None:
    _, plant, _, _ = prepared(monkeypatch)
    with pytest.raises(ValueError, match=r"required|exist|requires past"):
        reconstruct_forecasts(
            [] if invalid == "empty" else [plant.pk + 1 if invalid == "plant" else plant.pk],
            DAY,
            DAY - timedelta(days=1)
            if invalid == "reversed"
            else NOW.date() + timedelta(days=1)
            if invalid == "future"
            else DAY,
            NOW.replace(tzinfo=None) if invalid == "naive" else NOW,
            apply=True,
        )
    assert not ForecastReconstruction.objects.exists()


def test_current_calibration_and_native_clipping_are_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, run, source = prepared(monkeypatch)
    run.factor = 3
    run.save()
    source.normalized_intervals[0]["power_kw"] = 0
    source.save()
    reconstruct_forecasts([plant.pk], DAY, DAY, NOW, apply=True)
    payload = outlook(system, plant)["result"]
    assert payload["energy_mwh"] == pytest.approx(2350)
    assert payload["intervals"][0]["mean_power_mw"] == 0
    assert max(row["mean_power_mw"] for row in payload["intervals"]) == 100


def test_frozen_previous_configuration_and_tenant_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant, _, _ = prepared(monkeypatch)
    reconstruct_forecasts([plant.pk], DAY, DAY, NOW, apply=True)
    Plant.objects.filter(pk=plant.pk).update(current_configuration=None)
    response = outlook(system, plant)
    assert response["freshness"] == "stale"
    assert "previous_configuration" in response["result"]["quality_flags"]
    tenant = Tenant.objects.create(name="Other")
    outsider = User.objects.create_user(username="outsider")
    Membership.objects.create(tenant=tenant, user=outsider, role=Membership.Role.ADMIN)
    with pytest.raises(PermissionDenied):
        plant_outlook(
            outsider,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            target_date=DAY,
            resolution_minutes=30,
            source="archived_forecast",
        )
    with pytest.raises(Region.DoesNotExist):
        plant_outlook(
            outsider,
            tenant.pk,
            system.region.pk,
            plant.pk,
            target_date=DAY,
            resolution_minutes=30,
            source="archived_forecast",
        )


def test_concurrent_reconstruction_and_database_immutability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plant, _, _ = prepared(monkeypatch)
    barrier = Barrier(2)

    def write() -> str:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return cast(
                str, reconstruct_forecasts([plant.pk], DAY, DAY, NOW, apply=True)[0]["status"]
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: write(), range(2)))
    assert sorted(results) == ["already_exists", "created"]
    assert ForecastReconstruction.objects.count() == 1
    assert PlantEvent.objects.filter(action="forecast_reconstructed").count() == 1
    saved = ForecastReconstruction.objects.get()
    with pytest.raises(IntegrityError, match="immutable"), transaction.atomic():
        ForecastReconstruction.objects.filter(pk=saved.pk).update(payloads={})
    saved.pk = None
    with (
        pytest.raises(IntegrityError, match="forecast_reconstruction_unique"),
        transaction.atomic(),
    ):
        saved.save(force_insert=True)
    saved.factor = 0.25
    with pytest.raises(IntegrityError, match="lineage"), transaction.atomic():
        saved.save(force_insert=True)


def test_reconstruction_is_purged_before_its_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant, _, source = prepared(monkeypatch)
    reconstruct_forecasts([plant.pk], DAY, DAY, NOW, apply=True)
    trash_plant(system.member, system.tenant.pk, system.region.pk, plant.pk)
    assert purge_expired(NOW + RETENTION) == {"regions": 0, "plants": 1}
    assert not ForecastReconstruction.objects.exists()
    assert ForecastSnapshot.objects.filter(pk=source.pk).exists()


def test_every_requested_day_and_plant_uses_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _, _ = prepared(monkeypatch)
    region = submit_region(
        system.member,
        system.tenant.pk,
        name="Unconfigured",
        latitude=41,
        longitude=30,
        timezone="Europe/Istanbul",
    )
    second = Plant.objects.create(region=region, name="Unconfigured")
    with pytest.raises(ValueError, match="usable activated calibration"):
        reconstruct_forecasts([plant.pk, second.pk], DAY, DAY, NOW, apply=True)
    assert not ForecastReconstruction.objects.exists()
    assert not PlantEvent.objects.filter(action="forecast_reconstructed").exists()


def test_source_availability_is_checked_at_reconstruction_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plant, run, source = prepared(monkeypatch)
    early = source.collected_at - timedelta(hours=1)
    CalibrationRun.objects.filter(pk=run.pk).update(
        created_at=early, cutoff_at=early, completed_at=early
    )
    CalibrationActivation.objects.filter(calibration=run).update(created_at=early)
    source.available_at = source.collected_at + timedelta(hours=3)
    source.save()
    assert (
        reconstruct_forecasts([plant.pk], DAY, DAY, source.collected_at + timedelta(hours=2))[0][
            "status"
        ]
        == "source_unavailable"
    )
    assert (
        reconstruct_forecasts([plant.pk], DAY, DAY, source.collected_at + timedelta(hours=4))[0][
            "status"
        ]
        == "preview"
    )


def test_newest_activated_calibration_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant, first, _ = prepared(monkeypatch)
    newest = CalibrationRun.objects.get(pk=first.pk)
    newest.pk = None
    newest.factor = 0.75
    newest.comparison_activation = CalibrationActivation.objects.get(calibration=first)
    newest.save()
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, newest.pk)
    reconstruct_forecasts([plant.pk], DAY, DAY, NOW, apply=True)
    saved = ForecastReconstruction.objects.get()
    assert saved.calibration_id == newest.pk
    assert saved.factor == 0.75
    assert saved.payloads["30"]["energy_mwh"] == pytest.approx(936)
