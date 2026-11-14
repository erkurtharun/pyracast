from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.secrets import encrypt_secret
from pyracast.forecasts.capacity_normalization import normalize_reference_capacity
from pyracast.forecasts.history import select_actual_snapshot
from pyracast.forecasts.models import ForecastSnapshot, ReferenceCapacityNormalization
from pyracast.plants.availability import actual_power_intervals
from pyracast.plants.models import PlantConfiguration
from tests.test_domain import NOW, System
from tests.test_plant_operations import make_plant, publish_snapshot, snapshot_for
from tests.test_plant_training import actual_snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


def scaled_system(monkeypatch: pytest.MonkeyPatch) -> tuple[System, SiteBinding]:
    system, _ = make_plant(monkeypatch)
    ProviderAccount.objects.filter(pk=system.account.pk).update(
        provider="solcast", encrypted_api_key=encrypt_secret("test-only-solcast-key")
    )
    SiteBinding.objects.filter(pk=system.binding.pk).update(
        settings_verification_reference="Verified original Hobby settings", dc_capacity_kw=1
    )
    target = SiteBinding.objects.get(pk=system.binding.pk)
    target.pk = None
    target.capacity_kw = target.dc_capacity_kw = 1000
    target.save()
    system.region.approved_binding = target
    system.region.save(update_fields=["approved_binding"])
    return system, target


def normalize(system: System, target: SiteBinding) -> tuple[int, ...]:
    return normalize_reference_capacity(
        system.admin,
        system.tenant.pk,
        system.region.pk,
        system.binding.pk,
        target.pk,
        "User confirms the same Hobby model with only a 1 to 1000 kW reference scale change",
    )


def test_old_hobby_history_becomes_available_at_the_new_scale_without_rewriting_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    day = date(2026, 11, 7)
    original = actual_snapshot_for(system, day, power_kw=0.6)
    original.raw_payload = {"estimated_actuals": [{"pv_estimate": 0.6}]}
    original.normalized_intervals[0]["power_kw"] = None
    original.normalized_intervals[1]["power_kw"] = 0
    original.normalized_intervals[2]["sunlight"] = "day"
    original.save()
    before = deepcopy(original.normalized_intervals)
    attempts = system.account.requestattempt_set.count()
    assert select_actual_snapshot(target, day, NOW) is None
    ids = normalize(system, target)
    selected = select_actual_snapshot(target, day, NOW)
    assert selected is not None
    assert ids == (selected.pk,)
    assert selected.normalized_intervals[0]["power_kw"] is None
    assert selected.normalized_intervals[1]["power_kw"] == 0
    assert selected.normalized_intervals[2] == before[2] | {"power_kw": 600}
    assert selected.raw_payload == original.raw_payload
    assert selected.collected_at == original.collected_at
    assert selected.available_at == NOW
    assert select_actual_snapshot(target, day, NOW - timedelta(microseconds=1)) is None
    original.refresh_from_db()
    assert original.normalized_intervals == before
    system.binding.refresh_from_db()
    assert system.binding.capacity_kw == 1
    assert system.binding.dc_capacity_kw == 1
    record = ReferenceCapacityNormalization.objects.get(snapshot=selected)
    assert record.source_snapshot == original
    assert record.factor == 1000
    assert record.source_capacity_kw == 1
    assert record.target_capacity_kw == 1000
    assert record.authorized_by == system.admin
    assert record.authorization_reference.startswith("User confirms")
    assert selected.job.status == "succeeded"
    assert selected.job.attempts == 0
    assert system.account.requestattempt_set.count() == attempts
    old_configuration = PlantConfiguration.objects.get(binding=system.binding)
    target_configuration = deepcopy(old_configuration)
    target_configuration.binding = target
    old_power = actual_power_intervals(original, old_configuration)
    new_power = actual_power_intervals(selected, target_configuration)
    assert [row.power_mw for row in new_power] == pytest.approx([row.power_mw for row in old_power])
    assert normalize(system, target) == ids
    assert ReferenceCapacityNormalization.objects.count() == 1


@pytest.mark.parametrize("missing_availability", [False, True])
def test_forecast_copy_recalculates_results_and_never_backdates_publication(
    monkeypatch: pytest.MonkeyPatch,
    missing_availability: bool,
) -> None:
    system, target = scaled_system(monkeypatch)
    original = publish_snapshot(snapshot_for(system, date(2026, 11, 9), power_kw=0.6))
    if missing_availability:
        original.available_at = None
        original.save()
    ids = normalize(system, target)
    assert len(ids) == 1
    copied = ForecastSnapshot.objects.get(pk=ids[0])
    assert copied.normalized_intervals[0]["power_kw"] == 600
    assert copied.forecastresult.published_at == NOW
    assert copied.forecastresult.payload["partial_energy_kwh_m2"] == pytest.approx(14.4)
    assert original.forecastresult.payload == {}


def test_regional_forecast_selects_the_normalized_copy_of_the_same_vintage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    publish_snapshot(snapshot_for(system, date(2026, 11, 9), power_kw=0.6))
    normalize(system, target)
    client = Client()
    client.force_login(system.member)
    response = client.get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/",
        {"date": "2026-11-09", "data_source": "forecast"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["binding_revision"] == target.pk
    assert body["result"]["reference_capacity_normalization"]["factor"] == 1000


@pytest.mark.parametrize("normalized_primary", [False, True])
def test_regional_composition_preserves_primary_normalization_and_each_source_identity(
    monkeypatch: pytest.MonkeyPatch, normalized_primary: bool
) -> None:
    system, target = scaled_system(monkeypatch)
    day = NOW.date()
    original = snapshot_for(
        system,
        day,
        power_kw=0.6,
        collected_at=NOW - timedelta(hours=1) if normalized_primary else None,
    )
    if normalized_primary:
        original.normalized_intervals = original.normalized_intervals[21:]
    publish_snapshot(original)
    (copied_id,) = normalize(system, target)
    native = snapshot_for(
        replace(system, binding=target),
        day,
        power_kw=600,
        collected_at=None if normalized_primary else NOW - timedelta(hours=1),
    )
    if not normalized_primary:
        native.normalized_intervals = native.normalized_intervals[21:]
    publish_snapshot(native)
    client = Client()
    client.force_login(system.member)
    response = client.get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/",
        {"date": day.isoformat(), "data_source": "forecast"},
    )
    assert response.status_code == 200
    payload = response.json()["result"]
    primary_id, earlier_id = (
        (copied_id, native.pk) if normalized_primary else (native.pk, copied_id)
    )
    assert payload["complete"] is True
    assert payload["energy_kwh_m2"] == pytest.approx(14.4)
    assert payload["provenance"]["snapshot_id"] == primary_id
    assert payload["provenance"]["source_snapshot_ids"] == [primary_id, earlier_id]
    assert payload["intervals"][20]["source_snapshot_ids"] == [earlier_id]
    assert payload["intervals"][21]["source_snapshot_ids"] == [primary_id]
    normalization = payload["reference_capacity_normalization"]
    if normalized_primary:
        assert normalization["source_snapshot_id"] == original.pk
        assert normalization["source_binding_id"] == system.binding.pk
        assert normalization["target_capacity_kw"] == 1000
        assert normalization["factor"] == 1000
    else:
        assert normalization is None


@pytest.mark.parametrize("reference", ["", " " * 3, "x" * 1001])
def test_scale_change_requires_an_explicit_bounded_authorization(
    monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    system, target = scaled_system(monkeypatch)
    with pytest.raises(ValidationError, match="authorization"):
        normalize_reference_capacity(
            system.admin,
            system.tenant.pk,
            system.region.pk,
            system.binding.pk,
            target.pk,
            reference,
        )
    assert not ReferenceCapacityNormalization.objects.exists()


@pytest.mark.parametrize("patch", [{"site_id": "another-site"}, {"losses_percent": 5}])
def test_scale_change_rejects_changes_to_model_inputs(
    monkeypatch: pytest.MonkeyPatch, patch: dict[str, Any]
) -> None:
    system, target = scaled_system(monkeypatch)
    SiteBinding.objects.filter(pk=target.pk).update(**patch)
    with pytest.raises(ValidationError):
        normalize(system, target)
    assert not ReferenceCapacityNormalization.objects.exists()


def test_non_admin_cannot_relabel_archived_reference_power(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, target = scaled_system(monkeypatch)
    with pytest.raises(PermissionDenied):
        normalize_reference_capacity(
            system.member,
            system.tenant.pk,
            system.region.pk,
            system.binding.pk,
            target.pk,
            "Same model, changed scale",
        )
