import json
from datetime import UTC, datetime, time, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from pyracast.accounts.models import Membership, Tenant
from pyracast.forecasts.history import HISTORY_SELECTION_POLICY
from pyracast.forecasts.models import CollectionJob, ForecastSnapshot
from pyracast.plants.models import (
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantForecast,
    ProductionObservation,
)
from pyracast.plants.numerics import POLICY_VERSION
from pyracast.plants.serializers import AwareDateTime, FiniteFloat
from tests.support.domain import make_system
from tests.support.plant_api import setup_plant
from tests.support.plant_operations import publish_snapshot

pytestmark = pytest.mark.django_db(transaction=True)


def test_mounting_geometry_is_validated_and_versioned_in_api() -> None:
    client, base, plant = setup_plant()
    configuration = plant["current_configuration"]
    assert configuration["mounting_type"] == "unknown"
    assert configuration["surface_tilt"] is None
    assert configuration["surface_azimuth"] is None
    values = {
        "name": "Fixed PV",
        "dc_capacity_mw": 130,
        "export_limit_mw": 100,
        "mounting_type": "fixed",
        "surface_tilt": 30,
        "surface_azimuth": 180,
    }
    detail = f"{base}{plant['id']}/"
    response = client.put(detail, values, content_type="application/json")
    assert response.status_code == 200, response.content
    fixed = response.json()["current_configuration"]
    assert fixed["mounting_type"] == "fixed"
    assert fixed["surface_tilt"] == 30
    assert fixed["surface_azimuth"] == 180
    assert fixed["export_limit_mw"] == 100
    assert fixed["inverter_capacity_mw"] is None
    for invalid in (
        {"mounting_type": "tracking"},
        {"mounting_type": "unknown"},
        {"mounting_type": "dual_axis"},
        {"surface_tilt": True},
        {"surface_tilt": 91},
        {"surface_azimuth": 360},
    ):
        response = client.put(detail, values | invalid, content_type="application/json")
        assert response.status_code == 400, response.content
        assert client.get(detail).json()["current_configuration"]["id"] == fixed["id"]
    response = client.put(
        detail,
        values | {"mounting_type": "tracking", "surface_tilt": None, "surface_azimuth": None},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    tracking = response.json()["current_configuration"]
    assert tracking["mounting_type"] == "tracking"
    assert tracking["surface_tilt"] is None
    assert tracking["surface_azimuth"] is None
    assert tracking["revision"] == fixed["revision"] + 1


def test_new_plant_forecast_reports_unavailable_history_without_rejecting_capacities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    now = datetime(2026, 11, 10, 7, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: now)
    client = Client()
    client.force_login(system.member)
    base = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/"
    created = client.post(
        base,
        {
            "name": "New plant",
            "dc_capacity_mw": 130,
            "inverter_capacity_mw": 100,
            "export_limit_mw": None,
            "measurement_basis": "net_export",
        },
        content_type="application/json",
    )
    assert created.status_code == 201
    plant = created.json()
    configuration = plant["current_configuration"]
    assert configuration["dc_capacity_mw"] == 130
    assert configuration["inverter_capacity_mw"] == 100
    assert configuration["export_limit_mw"] is None
    detail = f"{base}{plant['id']}/forecast/"
    today = client.get(detail, {"date": "2026-11-10", "resolution_minutes": "30"})
    assert today.status_code == 200, today.content
    assert today.json()["freshness"] == "missing"
    assert today.json()["unavailable_reason"] == "forecast_unavailable"
    assert today.json()["configuration_id"] == configuration["id"]
    assert today.json()["calibration_id"] is None
    assert today.json()["result"] is None
    assert today.json()["regional_irradiance"] is None
    assert today.json()["timezone"] == "Europe/Istanbul"
    assert datetime.fromisoformat(today.json()["as_of"]) == now
    tomorrow = client.get(detail, {"date": "2026-11-11", "resolution_minutes": "60"})
    assert tomorrow.status_code == 200
    assert tomorrow.json()["configuration_id"] == configuration["id"]
    assert tomorrow.json()["unavailable_reason"] == "forecast_unavailable"
    assert tomorrow.json()["result"] is None
    assert not PlantForecast.objects.exists()


def test_tenant_scope_observation_import_and_validation() -> None:
    client, base, plant = setup_plant()
    detail = f"{base}{plant['id']}/"
    assert client.get(base).json()["count"] == 1
    assert client.get(detail).json()["current_configuration"]["revision"] == 1
    config = plant["current_configuration"]["id"]
    observation = {
        "starts_at": "2020-01-01T09:00:00Z",
        "ends_at": "2020-01-01T09:30:00Z",
        "energy": 500,
        "quality": "valid",
        "source": "meter",
    }
    payload = {"configuration_id": config, "unit": "kWh", "observations": [observation]}
    response = client.post(detail + "observations/", payload, content_type="application/json")
    assert response.status_code == 201, response.content
    assert response.json()["created"] == 1
    assert ProductionObservation.objects.get().energy_mwh == 0.5
    assert (
        client.post(detail + "observations/", payload, content_type="application/json").json()[
            "existing"
        ]
        == 1
    )
    assert client.get(detail + "observations/").json()["results"][0]["energy_mwh"] == 0.5
    for change in (
        {"energy": True},
        {"energy": "NaN"},
        {"starts_at": "2020-01-01T09:00:00"},
        {"extra": 1},
    ):
        invalid = {**payload, "observations": [{**observation, **change}]}
        assert (
            client.post(
                detail + "observations/", invalid, content_type="application/json"
            ).status_code
            == 400
        )
    assert (
        client.post(
            detail + "observations/", {**payload, "unit": "watts"}, content_type="application/json"
        ).status_code
        == 400
    )
    current = PlantConfiguration.objects.get(pk=config)
    other_plant = Plant.objects.create(
        region_id=plant["region_id"], name="Other PV", deleted_at=timezone.now()
    )
    foreign_config = PlantConfiguration.objects.create(
        region_id=other_plant.region_id,
        plant=other_plant,
        binding=current.binding,
        revision=1,
        dc_capacity_mw=2,
        created_by=current.created_by,
    ).pk
    assert (
        client.post(
            detail + "observations/",
            {**payload, "configuration_id": foreign_config},
            content_type="application/json",
        ).status_code
        == 404
    )
    assert (
        client.put(
            detail,
            {"name": "PV", "dc_capacity_mw": 1, "tenant_id": 1},
            content_type="application/json",
        ).status_code
        == 400
    )
    other = Tenant.objects.create(name="Other")
    user = User.objects.create_user(username="other")
    Membership.objects.create(tenant=other, user=user)
    client.force_login(user)
    assert client.get(detail).status_code == 403
    assert (
        client.post(detail + "observations/", payload, content_type="application/json").status_code
        == 403
    )
    own_scope = f"/api/tenants/{other.pk}/regions/{plant['region_id']}/plants/{plant['id']}/"
    assert client.get(own_scope).status_code == 404
    assert client.get(own_scope + "calibrations/").status_code == 404


def test_calibration_pending_forecast_and_csrf() -> None:
    client, base, plant = setup_plant()
    detail = f"{base}{plant['id']}/"
    end = timezone.now().date() - timedelta(days=2)
    response = client.post(
        detail + "calibrations/",
        {
            "configuration_id": plant["current_configuration"]["id"],
            "start_date": (end - timedelta(days=6)).isoformat(),
            "end_date": end.isoformat(),
        },
        content_type="application/json",
    )
    assert response.status_code == 202, response.content
    run = response.json()
    assert run["status"] == "pending"
    assert run["completed_at"] is None
    assert CalibrationRun.objects.get().status == "pending"
    report = client.get(detail + f"calibrations/{run['id']}/").json()
    assert report["id"] == run["id"]
    assert report["dataset_manifest"] == {}
    listing = client.get(detail + "calibrations/").json()
    assert listing["count"] == 1
    assert "dataset_manifest" not in listing["results"][0]
    assert (
        client.post(
            detail + f"calibrations/{run['id']}/activate/", {}, content_type="application/json"
        ).status_code
        == 400
    )
    assert (
        client.post(
            detail + "calibration/reset/", {"extra": 1}, content_type="application/json"
        ).status_code
        == 400
    )
    assert client.get(detail + "forecast/?date=2020-01-01&resolution_minutes=15").status_code == 400
    assert (
        client.get(detail + "forecast/?date=2020-01-01&as_of=2020-01-01T09:00:00").status_code
        == 400
    )
    saved = CalibrationRun.objects.get()
    saved.status = "ready"
    saved.factor = 1.0
    saved.assessment = "provisional"
    saved.completed_at = timezone.now()
    saved.result = {
        "policy_version": "regularized-huber-v3",
        "holdout_days": 1,
        "candidate": {"mae": 1},
        "baseline": {"mae": 2},
    }
    saved.dataset_manifest = {
        "selection_policy": "first-available-day-ahead-v1",
        "source_kind": "forecast",
    }
    saved.save()
    assert (
        client.post(
            detail + f"calibrations/{run['id']}/activate/", {}, content_type="application/json"
        ).status_code
        == 400
    )
    saved.dataset_manifest = {
        "selection_policy": HISTORY_SELECTION_POLICY,
        "source_kind": "estimated_actuals",
    }
    saved.save(update_fields=("dataset_manifest",))
    assert (
        client.post(
            detail + f"calibrations/{run['id']}/activate/", {}, content_type="application/json"
        ).status_code
        == 400
    )
    saved.result["policy_version"] = POLICY_VERSION
    saved.save(update_fields=("result",))
    activation = client.post(
        detail + f"calibrations/{run['id']}/activate/", {}, content_type="application/json"
    )
    assert activation.status_code == 200
    assert activation.json()["calibration_id"] == run["id"]
    reset = client.post(detail + "calibration/reset/", {}, content_type="application/json")
    assert reset.status_code == 200
    assert reset.json()["calibration_id"] is None
    assert (
        client.put(
            detail, {"name": "PV", "dc_capacity_mw": True}, content_type="application/json"
        ).status_code
        == 400
    )
    guarded = Client(enforce_csrf_checks=True)
    guarded.force_login(User.objects.get(username="member"))
    assert (
        guarded.put(
            detail, {"name": "PV", "dc_capacity_mw": 1}, content_type="application/json"
        ).status_code
        == 403
    )


def test_forecast_resolution_caps_native_intervals() -> None:
    client, base, plant = setup_plant()
    detail = f"{base}{plant['id']}/"
    response = client.put(
        detail,
        {"name": "PV", "dc_capacity_mw": 1, "export_limit_mw": 0.5},
        content_type="application/json",
    )
    assert response.status_code == 200
    configuration = Plant.objects.get(pk=plant["id"]).current_configuration
    assert configuration is not None
    now = timezone.now()
    target = now.astimezone(ZoneInfo("Europe/Istanbul")).date() + timedelta(days=1)
    start = datetime.combine(target, time.min, ZoneInfo("Europe/Istanbul"))
    job = CollectionJob.objects.create(
        binding=configuration.binding, target_date=target, scheduled_slot=now, available_at=now
    )
    snapshot = ForecastSnapshot.objects.create(
        job=job,
        binding=configuration.binding,
        source_kind="forecast",
        collected_at=now,
        raw_payload={},
        normalized_intervals=[
            {
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=30)).isoformat(),
                "power_kw": 1,
            },
            {
                "start": (start + timedelta(minutes=30)).isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
                "power_kw": 0.2,
            },
        ],
    )
    publish_snapshot(snapshot)
    query = f"{detail}forecast/?date={target.isoformat()}"
    half = client.get(query + "&resolution_minutes=30")
    hour = client.get(query + "&resolution_minutes=60")
    assert half.status_code == 200
    assert hour.status_code == 200
    assert half.json()["result"]["intervals"][0]["energy_mwh"] == 0.25
    assert hour.json()["result"]["intervals"][0]["energy_mwh"] == pytest.approx(0.35)


def test_numeric_and_datetime_fields_reject_invalid_values() -> None:
    for value in (float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            FiniteFloat().run_validation(value)
    for timestamp in (42, "2020-99-01T00:00:00Z", "nonsense"):
        with pytest.raises(ValidationError):
            AwareDateTime().run_validation(timestamp)


def test_openapi_has_distinct_forecast_and_status_components() -> None:
    output = StringIO()
    call_command(
        "spectacular", format="openapi-json", validate=True, fail_on_warn=True, stdout=output
    )
    schema = json.loads(output.getvalue())
    components = schema["components"]["schemas"]
    assert "regional_irradiance" in components["PlantForecastOutput"]["properties"]
    assert "region_id" in components["ForecastOutput"]["properties"]
    assert "ready" in components["CalibrationStatusEnum"]["enum"]
    assert "approved" not in components["CalibrationStatusEnum"]["enum"]
