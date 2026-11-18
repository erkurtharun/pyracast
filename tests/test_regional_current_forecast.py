from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import ForecastResult, ForecastSnapshot, RequestAttempt
from pyracast.regions.models import Region
from tests.support.domain import NOW, make_system
from tests.support.invariants import stored_again
from tests.support.plant_operations import publish_snapshot, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("source", ["forecast", "auto"])
def test_region_current_forecast_composes_saved_horizon_without_republishing(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    day = NOW.date()
    old = publish_snapshot(snapshot_for(system, day, power_kw=0.1))
    latest = snapshot_for(system, day, power_kw=0.2, collected_at=NOW - timedelta(hours=1))
    latest.normalized_intervals = latest.normalized_intervals[21:]
    latest.job.target_date = day + timedelta(days=1)
    latest.job.save(update_fields=["target_date"])
    publish_snapshot(latest)
    counts = (
        ForecastSnapshot.objects.count(),
        ForecastResult.objects.count(),
        RequestAttempt.objects.count(),
    )
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    response = client.get(path, {"date": day.isoformat(), "data_source": source})
    assert response.status_code == 200, response.content
    payload = response.json()["result"]
    assert payload["complete"] is True
    assert payload["energy_kwh_m2"] == pytest.approx(3.75)
    assert payload["intervals"][20]["source_snapshot_ids"] == [old.pk]
    assert payload["intervals"][21]["source_snapshot_ids"] == [latest.pk]
    assert payload["provenance"]["source_snapshot_ids"] == [latest.pk, old.pk]
    assert payload["vintage"]["scope"] == "primary_forecast_source"
    assert "mixed_forecast_vintages" in payload["quality_flags"]
    assert counts == (
        ForecastSnapshot.objects.count(),
        ForecastResult.objects.count(),
        RequestAttempt.objects.count(),
    )


@pytest.mark.parametrize("days_ahead", [1, 2])
def test_region_forecast_reads_later_days_in_saved_provider_horizon(
    monkeypatch: pytest.MonkeyPatch, days_ahead: int
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    day = NOW.date() + timedelta(days=days_ahead)
    snapshot = snapshot_for(system, day, power_kw=0.2, collected_at=NOW - timedelta(hours=1))
    snapshot.job.target_date = NOW.date()
    snapshot.job.save(update_fields=["target_date"])
    publish_snapshot(snapshot)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    response = client.get(path, {"date": day.isoformat()})
    assert response.status_code == 200
    assert response.json()["result"]["local_date"] == day.isoformat()
    assert response.json()["result"]["energy_kwh_m2"] == pytest.approx(4.8)


@pytest.mark.parametrize(
    "field", ["collected_at", "available_at", "issued_at", "published_at", "failed_job"]
)
def test_region_current_forecast_excludes_unpublished_or_unsuccessful_sources(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    day = NOW.date()
    old = publish_snapshot(snapshot_for(system, day, power_kw=0.1))
    latest = publish_snapshot(
        snapshot_for(system, day, power_kw=0.2, collected_at=NOW - timedelta(hours=1))
    )
    if field == "published_at":
        ForecastResult.objects.filter(snapshot=latest).update(
            published_at=NOW + timedelta(seconds=1)
        )
    elif field == "failed_job":
        latest.job.status = "failed"
        latest.job.save(update_fields=["status"])
    else:
        setattr(latest, field, NOW + timedelta(seconds=1))
        stored_again(latest)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    response = client.get(path, {"date": day.isoformat()})
    assert response.status_code == 200
    assert response.json()["result"]["provenance"]["source_snapshot_ids"] == [old.pk]
    assert response.json()["result"]["energy_kwh_m2"] == pytest.approx(2.4)


def test_region_current_forecast_preserves_newer_missing_values_and_zeroes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    day = NOW.date()
    publish_snapshot(snapshot_for(system, day, power_kw=0.1))
    latest = snapshot_for(system, day, power_kw=0.2, collected_at=NOW - timedelta(hours=1))
    latest.normalized_intervals = latest.normalized_intervals[21:]
    latest.normalized_intervals[12]["power_kw"] = None
    latest.normalized_intervals[13]["power_kw"] = 0
    latest.normalized_intervals[-1]["power_kw"] = 0
    latest.normalized_intervals[-1]["sunlight"] = "night"
    publish_snapshot(latest)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    payload = client.get(path, {"date": day.isoformat()}).json()["result"]
    assert payload["complete"] is False
    assert payload["energy_kwh_m2"] is None
    assert payload["partial_energy_kwh_m2"] == pytest.approx(3.45)
    assert payload["intervals"][33]["ghi_wm2"] is None
    assert payload["intervals"][34]["ghi_wm2"] is None
    assert "ambiguous_zero_power" in payload["intervals"][34]["quality_flags"]
    assert payload["intervals"][-1]["ghi_wm2"] == 0


@pytest.mark.parametrize("change", ["no_binding", "replacement_binding", "no_snapshot"])
def test_region_current_forecast_never_reads_a_superseded_binding(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    day = NOW.date()
    if change != "no_snapshot":
        publish_snapshot(snapshot_for(system, day))
    if change == "no_binding":
        Region.objects.filter(pk=system.region.pk).update(status="pending", approved_binding=None)
    elif change == "replacement_binding":
        replacement = SiteBinding.objects.create(
            region=system.region,
            provider_account=system.account,
            region_revision=system.region.revision + 1,
            site_id="replacement",
            capacity_kw=1,
            capacity_semantics_verified=True,
            latitude=system.region.latitude,
            longitude=system.region.longitude,
            timezone=system.region.timezone,
        )
        Region.objects.filter(pk=system.region.pk).update(
            approved_binding=replacement, revision=replacement.region_revision
        )
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    result = client.get(path, {"date": day.isoformat()}).json()
    assert result["freshness"] == "missing"
    assert result["result"] is None
    assert result["unavailable_reason"] == "forecast_unavailable"


@pytest.mark.parametrize("change", ["old_contribution", "invalid_binding"])
def test_region_current_forecast_marks_any_old_or_invalid_source_stale(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    day = NOW.date()
    publish_snapshot(snapshot_for(system, day, collected_at=NOW - timedelta(hours=25)))
    latest = snapshot_for(system, day, collected_at=NOW - timedelta(hours=1))
    latest.normalized_intervals = (
        latest.normalized_intervals[21:]
        if change == "old_contribution"
        else latest.normalized_intervals
    )
    publish_snapshot(latest)
    if change == "invalid_binding":
        system.account.enabled = False
        system.account.save(update_fields=["enabled"])
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    result = client.get(path, {"date": day.isoformat()}).json()
    assert result["freshness"] == "stale"
    assert result["result"]["complete"] is True


@pytest.mark.parametrize("error", [ValueError, ArithmeticError])
def test_region_current_forecast_maps_invalid_saved_inputs_to_validation_error(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    publish_snapshot(snapshot_for(system, NOW.date()))

    def invalid_calculation(*args: object) -> dict[str, object]:
        raise error("Invalid archived source")

    monkeypatch.setattr("pyracast.forecasts.history.calculate", invalid_calculation)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    response = client.get(path, {"date": NOW.date().isoformat()})
    assert response.status_code == 400
    assert response.json() == {
        "code": "calculation_out_of_range",
        "message": (
            "This forecast could not be calculated. "
            "Choose another day or contact your administrator."
        ),
    }
