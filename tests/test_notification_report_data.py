from datetime import datetime, timedelta
from typing import Any, cast

import pytest
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.metering.models import EpiasConnection
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.preparation import prepare_delivery
from pyracast.notifications.report_data import saved_report_context
from pyracast.notifications.scheduler import enqueue_due
from pyracast.plants.models import CalibrationActivation, Plant, PlantConfiguration
from pyracast.plants.operations import create_plant
from pyracast.regions.models import Region
from tests.test_domain import NOW, System
from tests.test_historical_production_identity import record_identity
from tests.test_incident_calibration import incident
from tests.test_notification_preparation import complete_saved
from tests.test_notifications import SLOT, configured, saved
from tests.test_plant_operations import ready_run
from tests.test_provisional_measurements import acquisition

pytestmark = pytest.mark.django_db(transaction=True)


def report_delivery(monkeypatch: pytest.MonkeyPatch) -> tuple[System, Plant, NotificationDelivery]:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system, _ = configured()
    plant = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        name="Electrical plant",
        dc_capacity_mw=130,
        export_limit_mw=100,
    )
    return system, plant, saved_delivery(system)


def region_delivery(monkeypatch: pytest.MonkeyPatch) -> tuple[System, NotificationDelivery]:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system, _ = configured()
    return system, saved_delivery(system)


def saved_delivery(system: System) -> NotificationDelivery:
    snapshot = complete_saved(system)
    delivery = NotificationDelivery.objects.get(pk=enqueue_due(SLOT)[0])
    delivery.snapshot = snapshot
    return delivery


def test_report_transfers_one_pinned_forecast_and_reported_outage_without_external_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, delivery = report_delivery(monkeypatch)
    monkeypatch.setattr(timezone, "now", lambda: SLOT - timedelta(minutes=1))
    outage = incident(system, plant, SLOT - timedelta(hours=2), None)
    counts = (
        RequestAttempt.objects.count(),
        CollectionJob.objects.count(),
        ForecastSnapshot.objects.count(),
    )
    context = saved_report_context(delivery)
    tomorrow = cast(list[dict[str, Any]], context["days"])[1]
    assert context["plant_id"] == plant.pk
    assert context["configuration_id"] == plant.current_configuration_id
    assert tomorrow["normal"]["energy_mwh"] == pytest.approx(936)
    assert tomorrow["outage"]["energy_mwh"] == pytest.approx(468)
    assert tomorrow["outage_status"] == "estimated"
    assert tomorrow["incident_revision_ids"] == [outage.revisions.get().pk]
    assert len(tomorrow["normal"]["intervals"]) == 24
    assert tomorrow["normal"]["provenance"]["snapshot_id"] == delivery.snapshot_id
    assert tomorrow["normal"]["provenance"]["binding_id"] == system.binding.pk
    assert counts == (
        RequestAttempt.objects.count(),
        CollectionJob.objects.count(),
        ForecastSnapshot.objects.count(),
    )


@pytest.mark.parametrize("unavailable", ["late_configuration", "other_binding", "damaged_source"])
def test_report_marks_electricity_unavailable_for_ineligible_inputs(
    monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    system, plant, delivery = report_delivery(monkeypatch)
    if unavailable == "late_configuration":
        assert plant.current_configuration_id is not None
        PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(
            created_at=SLOT + timedelta(seconds=1)
        )
    elif unavailable == "other_binding":
        region = Region.objects.create(
            tenant=Tenant.objects.create(name="Other tenant"),
            name="Other region",
            latitude=40,
            longitude=29,
            timezone=system.binding.timezone,
        )
        other = SiteBinding.objects.create(
            region=region,
            provider_account=system.account,
            region_revision=1,
            site_id="other",
            capacity_kw=1000,
            latitude=40,
            longitude=29,
            timezone=system.binding.timezone,
        )
        assert delivery.snapshot is not None
        delivery.snapshot.binding = other
    else:
        assert delivery.snapshot is not None
        delivery.snapshot.normalized_intervals = [{"invalid": True}]
    context = saved_report_context(delivery)
    assert context["plant_id"] == plant.pk
    assert all(day["normal"] is None for day in cast(list[dict[str, Any]], context["days"]))


def test_partial_source_preserves_missing_totals_and_valid_electrical_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _, delivery = report_delivery(monkeypatch)
    delivery.snapshot = saved(system, NOW + timedelta(seconds=1))
    context = saved_report_context(delivery)
    for day in cast(list[dict[str, Any]], context["days"]):
        assert day["normal"]["energy_mwh"] is None
        assert day["normal"]["partial_energy_mwh"] == pytest.approx(19.5)
        assert day["normal"]["complete"] is False
        assert day["outage"] is None


def test_report_excludes_incidents_and_calibration_activated_after_original_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, delivery = report_delivery(monkeypatch)
    run = ready_run(system, plant)
    monkeypatch.setattr(timezone, "now", lambda: SLOT + timedelta(minutes=1))
    assert plant.current_configuration is not None
    CalibrationActivation.objects.create(
        plant=plant, configuration=plant.current_configuration, calibration=run
    )
    incident(system, plant, SLOT - timedelta(hours=1), None)
    context = saved_report_context(delivery)
    tomorrow = cast(list[dict[str, Any]], context["days"])[1]
    assert tomorrow["normal"]["energy_mwh"] == pytest.approx(936)
    assert tomorrow["normal"]["provenance"]["calibration_id"] is None
    assert tomorrow["outage"] is None


def test_report_hours_integrate_native_daylight_scope_before_aggregation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, delivery = report_delivery(monkeypatch)
    monkeypatch.setattr(timezone, "now", lambda: SLOT - timedelta(minutes=1))
    incident(system, plant, SLOT - timedelta(hours=1), None)
    initial = saved_report_context(delivery)
    tomorrow = cast(list[dict[str, Any]], initial["days"])[1]
    sunrise = datetime.fromisoformat(tomorrow["normal"]["daylight"]["sunrise"])
    hour = sunrise.replace(minute=0, second=0, microsecond=0)
    before, after = sunrise - timedelta(minutes=2), sunrise + timedelta(minutes=2)
    assert hour < before < after < hour + timedelta(hours=1)
    assert delivery.snapshot is not None
    rows = delivery.snapshot.normalized_intervals
    original = next(row for row in rows if datetime.fromisoformat(row["start"]) == hour)
    rows.remove(original)
    rows.extend(
        {"start": left.isoformat(), "end": right.isoformat(), "power_kw": power}
        for left, right, power in (
            (hour, before, 1000),
            (before, after, 0.3),
            (after, hour + timedelta(hours=1), 0.6),
        )
    )
    rows.sort(key=lambda row: row["start"])
    delivery.snapshot.save(update_fields=("normalized_intervals",))
    result = saved_report_context(delivery)
    tomorrow = cast(list[dict[str, Any]], result["days"])[1]
    expected = (
        39 * (after - before).total_seconds() / 3600
        + 78 * (hour + timedelta(hours=1) - after).total_seconds() / 3600
    )
    first = tomorrow["normal"]["report_intervals"][0]
    assert datetime.fromisoformat(first["start"]) == before
    assert first["energy_mwh"] == pytest.approx(expected)
    for key in ("normal", "outage"):
        normal = tomorrow[key]
        assert sum(
            row["partial_energy_mwh"] for row in normal["report_intervals"]
        ) == pytest.approx(normal["daylight"]["partial_energy_mwh"])


def test_report_uses_eligible_calibration_and_exposes_missing_daylight_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, delivery = report_delivery(monkeypatch)
    run = ready_run(system, plant)
    assert plant.current_configuration is not None
    CalibrationActivation.objects.create(
        plant=plant, configuration=plant.current_configuration, calibration=run
    )
    initial = saved_report_context(delivery)
    tomorrow = cast(list[dict[str, Any]], initial["days"])[1]
    assert tomorrow["normal"]["energy_mwh"] == pytest.approx(468)
    assert tomorrow["normal"]["provenance"]["calibration_id"] == run.pk
    middle = tomorrow["normal"]["report_intervals"][5]
    assert delivery.snapshot is not None
    delivery.snapshot.normalized_intervals = [
        row for row in delivery.snapshot.normalized_intervals if row["start"] != middle["start"]
    ]
    delivery.snapshot.save(update_fields=("normalized_intervals",))
    context = saved_report_context(delivery)
    normal = cast(list[dict[str, Any]], context["days"])[1]["normal"]
    missing = next(row for row in normal["report_intervals"] if row["start"] == middle["start"])
    assert missing["energy_mwh"] is None
    assert missing["coverage_fraction"] == 0
    assert normal["daylight"]["energy_mwh"] is None
    assert sum(row["partial_energy_mwh"] for row in normal["report_intervals"]) == pytest.approx(
        normal["daylight"]["partial_energy_mwh"]
    )


def test_report_rejects_calibration_when_production_identity_was_wrong_at_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, delivery = report_delivery(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    record_identity(monkeypatch, connection, NOW)
    production = acquisition((configuration, connection))
    run = ready_run(system, plant)
    run.production_snapshot_ids = [production.pk]
    run.save(update_fields=("production_snapshot_ids",))
    CalibrationActivation.objects.create(plant=plant, configuration=configuration, calibration=run)
    initial = cast(list[dict[str, Any]], saved_report_context(delivery)["days"])[1]["normal"]
    assert initial["energy_mwh"] == pytest.approx(468)
    assert initial["provenance"]["calibration_id"] == run.pk
    connection.provider_plant_id = 3
    connection.revision += 1
    record_identity(monkeypatch, connection, SLOT - timedelta(minutes=1))
    connection.provider_plant_id = 2
    connection.revision += 1
    record_identity(monkeypatch, connection, SLOT + timedelta(minutes=1))
    context = saved_report_context(delivery)
    tomorrow = cast(list[dict[str, Any]], context["days"])[1]
    assert tomorrow["normal"]["energy_mwh"] == pytest.approx(936)
    assert tomorrow["normal"]["provenance"]["calibration_id"] is None


def test_unknown_reported_outage_keeps_normal_estimate_and_missing_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, delivery = report_delivery(monkeypatch)
    monkeypatch.setattr(timezone, "now", lambda: SLOT - timedelta(minutes=1))
    incident(system, plant, SLOT - timedelta(hours=1), None, available_dc_mw=None)
    context = saved_report_context(delivery)
    tomorrow = cast(list[dict[str, Any]], context["days"])[1]
    assert tomorrow["normal"]["energy_mwh"] == pytest.approx(936)
    assert tomorrow["outage_status"] == "unknown_impact"
    assert tomorrow["outage"]["daylight"]["energy_mwh"] is None
    assert all(row["energy_mwh"] is None for row in tomorrow["outage"]["report_intervals"])


def test_frozen_report_survives_later_input_changes_and_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, delivery = report_delivery(monkeypatch)
    assert prepare_delivery(delivery, SLOT)
    assert delivery.prepared_message is not None
    original = delivery.prepared_message["report_context"]
    Plant.objects.filter(pk=plant.pk).update(name="Later name")
    assert plant.current_configuration_id is not None
    PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(dc_capacity_mw=260)
    ForecastResult.objects.filter(snapshot=delivery.snapshot).update(daily_payloads={})
    assert delivery.snapshot is not None
    ForecastSnapshot.objects.filter(pk=delivery.snapshot.pk).update(normalized_intervals=[])
    prepared = delivery.prepared_message
    assert prepare_delivery(delivery, SLOT + timedelta(minutes=3))
    assert delivery.prepared_message == prepared
    assert cast(dict[str, Any], original)["plant_name"] == "Electrical plant"
    assert system.binding.pk == delivery.binding_id
