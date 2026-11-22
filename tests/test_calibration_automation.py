from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier, Event, current_thread
from typing import Any, cast

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.accounts.operations import reassign_workspace_admin
from pyracast.errors import DomainError
from pyracast.irradiance import local_day_bounds
from pyracast.plants.activation import activate_calibration, reset_calibration
from pyracast.plants.automation import (
    automation_status,
    prepare_due_calibrations,
    set_calibration_automation,
)
from pyracast.plants.calibration_data import current_source_validation
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantEvent,
    ProductionObservation,
)
from pyracast.plants.operations import revise_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.plants.training import run_calibration
from pyracast.regions.models import Region
from pyracast.trash import purge_expired, trash_region
from tests.support.calibration_automation import (
    declare_native_sources,
    evaluated_run,
    import_fit_history,
    promote,
)
from tests.support.domain import NOW, System
from tests.support.plant_operations import make_plant, ready_run
from tests.support.plant_training import actual_snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


def test_automation_can_start_before_a_fit_and_disable_without_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    client = Client()
    client.force_login(system.member)
    path = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/"
        f"{plant.pk}/calibration/automation/"
    )
    response = client.patch(path, {"enabled": True}, content_type="application/json")
    assert response.status_code == 200, response.content
    data = cast(dict[str, Any], response.json())
    assert data["enabled"] is True
    assert data["authorized"] is True
    assert data["next_check_at"] is not None
    assert client.get(path).json()["enabled"] is True
    assert not CalibrationActivation.objects.exists()
    response = client.patch(path, {"enabled": False}, content_type="application/json")
    assert response.status_code == 200, response.content
    assert response.json()["enabled"] is False
    assert not CalibrationActivation.objects.exists()


@pytest.mark.parametrize(
    ("ages", "accepted"),
    [
        ([180, 179, 178, 177, 176, 175, 174], False),
        ([70, 60, 50, 40, 30, 20, 1], False),
        ([15, 13, 12, 11, 10, 9, 1], False),
        ([14, 13, 12, 11, 10, 9, 8], False),
        ([14, 13, 12, 11, 10, 8, 7], True),
        ([14, 13, 12, 11, 10, 9, 1], True),
    ],
)
def test_automatic_evaluation_requires_recent_holdout_but_manual_selection_remains_explicit(
    monkeypatch: pytest.MonkeyPatch, ages: list[int], accepted: bool
) -> None:
    system, plant = make_plant(monkeypatch)
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )
    run = evaluated_run(system, plant)
    holdout = [NOW.date() - timedelta(days=age) for age in ages]
    training = [holdout[0] - timedelta(days=offset) for offset in range(23, 0, -1)]
    included = [day.isoformat() for day in training + holdout]
    run.window_start, run.window_end = training[0], holdout[-1]
    run.result.update(
        {
            "included_day_dates": included,
            "training_day_dates": [day.isoformat() for day in training],
            "holdout_day_dates": [day.isoformat() for day in holdout],
        }
    )
    source = next(iter(run.dataset_manifest["sources"].values()))
    run.dataset_manifest["sources"] = {
        day: source | {"snapshot_id": index + 1} for index, day in enumerate(included)
    }
    run.result["current_source_validation"] = current_source_validation(
        run.dataset_manifest, run.result, run.binding_id
    )
    assert run.result["current_source_validation"]["eligible"]
    run.save()
    assert promote(plant.pk, run) is accepted
    if not accepted:
        assert not CalibrationActivation.objects.exists()
        assert PlantEvent.objects.latest("pk").details["reason"] == "stale_automatic_evaluation"
        activation = activate_calibration(
            system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk
        )
        assert activation.calibration_id == run.pk


def unpublished_history(system: System, plant: Plant) -> None:
    observations = []
    for offset in range(7):
        day = date(2026, 11, 1) + timedelta(days=offset)
        actual_snapshot_for(system, day)
        start, end = local_day_bounds(day, system.region.timezone)
        while start < end:
            observations.append(
                ProductionObservation(
                    plant=plant,
                    configuration=plant.current_configuration,
                    starts_at=start,
                    ends_at=start + timedelta(hours=1),
                    energy_mwh=26,
                    source="meter",
                    recorded_by=system.member,
                )
            )
            start += timedelta(hours=1)
    ProductionObservation.objects.bulk_create(observations)


def test_activation_enables_updates_disable_keeps_factor_and_reset_stops_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = ready_run(system, plant)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    activation = activate_calibration(*scope, run.pk)
    plant.refresh_from_db()
    assert plant.automatic_calibration_enabled
    assert activate_calibration(*scope, run.pk).pk == activation.pk
    assert PlantEvent.objects.filter(action="calibration_automation_enabled").count() == 1
    set_calibration_automation(*scope, enabled=False)
    assert CalibrationActivation.objects.latest("pk").pk == activation.pk
    activate_calibration(*scope, run.pk)
    reset = reset_calibration(*scope)
    plant = Plant.objects.get(pk=plant.pk)
    assert reset.calibration_id is None
    assert not plant.automatic_calibration_enabled
    assert plant.automatic_calibration_enabled_by_id is None
    assert plant.next_calibration_check_at is None


def test_configuration_revision_requires_renewed_automation_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    set_calibration_automation(*scope, enabled=True)
    revise_plant(*scope, PlantParameters(name="Changed", dc_capacity_mw=140))
    plant.refresh_from_db()
    assert not plant.automatic_calibration_enabled
    assert plant.next_calibration_check_at is None


def test_ready_evaluated_fit_activates_automatically_once_with_system_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = evaluated_run(system, plant)
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )
    assert promote(plant.pk, run)
    activation = CalibrationActivation.objects.get()
    assert activation.calibration_id == run.pk
    assert activation.actor_id is None
    event = PlantEvent.objects.get(action="calibration_automatically_activated")
    assert event.details["origin"] == "automatic"
    assert event.details["enabled_by_id"] == system.member.pk
    assert promote(plant.pk, run)
    assert CalibrationActivation.objects.count() == 1


@pytest.mark.parametrize(
    "source_state",
    [
        "historical_only",
        "mixed_holdout",
        "legacy",
        "boolean_only",
        "wrong_binding",
        "false_summary",
        "missing_summary",
        "nonfinal_holdout",
        "dates_outside_window",
    ],
)
def test_automatic_promotion_requires_reproducible_current_source_evidence(
    monkeypatch: pytest.MonkeyPatch,
    source_state: str,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = evaluated_run(system, plant)
    sources = run.dataset_manifest["sources"]
    if source_state == "historical_only":
        for source in sources.values():
            source["source_product"] = "historical_rooftop_pv"
            source["reference_import"] = {"id": 1, "sha256": "a" * 64}
    elif source_state == "mixed_holdout":
        run.window_start = date(2026, 9, 1)
        declare_native_sources(run)
        source = run.dataset_manifest["sources"]["2026-11-07"]
        source["source_product"] = "historical_rooftop_pv"
        source["reference_import"] = {"id": 1, "sha256": "a" * 64}
    elif source_state == "legacy":
        run.dataset_manifest.pop("sources")
    elif source_state == "boolean_only":
        run.dataset_manifest["sources"] = {}
        run.result["current_source_validation"] = {
            "eligible": True,
            "valid_days": 30,
            "holdout_days": 7,
        }
    elif source_state == "wrong_binding":
        for source in sources.values():
            source["binding_id"] = run.binding_id + 1
    elif source_state == "false_summary":
        run.result["current_source_validation"]["valid_days"] = 1000
    elif source_state == "missing_summary":
        run.result.pop("current_source_validation")
    elif source_state == "nonfinal_holdout":
        run.result["holdout_day_dates"] = [date(2026, 10, day).isoformat() for day in range(9, 16)]
    else:
        run.window_start = date(2026, 10, 10)
    if source_state not in {"boolean_only", "false_summary", "missing_summary"}:
        run.result["current_source_validation"] = current_source_validation(
            run.dataset_manifest, run.result, run.binding_id
        )
    run.save()
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )
    assert not promote(plant.pk, run)
    assert not CalibrationActivation.objects.exists()
    assert (
        PlantEvent.objects.latest("pk").details["reason"]
        == "insufficient_current_source_validation"
    )


@pytest.mark.parametrize("malformed", [None, [42], ["invalid"], ["2026-10-01"] * 30, ["20261001"]])
def test_invalid_or_duplicate_included_days_cannot_establish_current_source_support(
    monkeypatch: pytest.MonkeyPatch,
    malformed: object,
) -> None:
    system, plant = make_plant(monkeypatch)
    run = evaluated_run(system, plant)
    run.result["included_day_dates"] = malformed
    run.result["current_source_validation"] = current_source_validation(
        run.dataset_manifest, run.result, run.binding_id
    )
    run.save()
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )
    assert not promote(plant.pk, run)
    assert not CalibrationActivation.objects.exists()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"assessment": "provisional"}, "insufficient_automatic_evaluation"),
        ({"valid_days": 29}, "insufficient_automatic_evaluation"),
        ({"holdout_day_dates": []}, "insufficient_automatic_evaluation"),
        ({"holdout_day_dates": ["invalid"]}, "insufficient_automatic_evaluation"),
        ({"candidate": {"daily_mae_mwh": 0.5}}, "invalid_incumbent_comparison"),
        (
            {"candidate": {"interval_mae_mw": 0.995, "daily_mae_mwh": 0.5}},
            "incumbent_improvement_below_margin",
        ),
        (
            {"candidate": {"interval_mae_mw": 0.5, "daily_mae_mwh": 0.995}},
            "incumbent_improvement_below_margin",
        ),
        (
            {
                "candidate": {"interval_mae_mw": 0.0, "daily_mae_mwh": 0.0},
                "incumbent": {"interval_mae_mw": 0.0, "daily_mae_mwh": 0.0},
            },
            "incumbent_improvement_below_margin",
        ),
        (
            {"candidate": {"interval_mae_mw": -1, "daily_mae_mwh": 0.5}},
            "invalid_incumbent_comparison",
        ),
        ({"status": "rejected"}, "candidate_rejected"),
    ],
)
def test_auto_guards_preserve_last_good_fit_and_expose_reason(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any], reason: str
) -> None:
    system, plant = make_plant(monkeypatch)
    run = evaluated_run(system, plant)
    for key, value in change.items():
        if key in {"assessment", "status"}:
            setattr(run, key, value)
        else:
            run.result[key] = value
    run.save()
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )
    assert not promote(plant.pk, run)
    assert not CalibrationActivation.objects.exists()
    plant.refresh_from_db()
    last = automation_status(plant)["last_attempt"]
    assert last["status"] == "skipped"
    assert last["reason"] == reason
    run.refresh_from_db()
    assert run.status == change.get("status", "ready")


def test_next_promotion_waits_for_seven_wholly_new_holdout_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    initial = evaluated_run(system, plant)
    initial.window_start, initial.window_end = date(2026, 10, 1), date(2026, 10, 30)
    initial.result["holdout_day_dates"] = [date(2026, 10, day).isoformat() for day in range(24, 31)]
    declare_native_sources(initial)
    initial.save()
    activation = activate_calibration(
        system.member, system.tenant.pk, system.region.pk, plant.pk, initial.pk
    )
    candidate = evaluated_run(system, plant)
    candidate.window_start = date(2026, 10, 1)
    assert candidate.comparison_activation_id == activation.pk
    candidate.window_end = date(2026, 11, 6)
    candidate.result["holdout_day_dates"] = [
        (date(2026, 10, 30) + timedelta(days=offset)).isoformat() for offset in range(7)
    ]
    candidate.window_end = date(2026, 11, 5)
    declare_native_sources(candidate)
    candidate.save()
    assert not promote(plant.pk, candidate)
    assert PlantEvent.objects.latest("pk").details["reason"] == "awaiting_new_holdout_days"
    candidate.result["holdout_day_dates"] = [
        (date(2026, 10, 31) + timedelta(days=offset)).isoformat() for offset in range(7)
    ]
    candidate.window_end = date(2026, 11, 6)
    declare_native_sources(candidate)
    candidate.save()
    assert promote(plant.pk, candidate)
    assert CalibrationActivation.objects.latest("pk").calibration_id == candidate.pk


@pytest.mark.parametrize("revocation", ["inactive", "membership", "demoted"])
def test_revoked_enabler_cannot_promote_or_prepare_daily_work(
    monkeypatch: pytest.MonkeyPatch, revocation: str
) -> None:
    system, plant = make_plant(monkeypatch)
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )
    candidate = evaluated_run(system, plant)
    if revocation == "inactive":
        system.member.is_active = False
        system.member.save(update_fields=("is_active",))
    elif revocation == "demoted":
        Membership.objects.filter(user=system.member).update(role=WorkspaceRole.VIEWER)
    else:
        Membership.objects.filter(user=system.member).delete()
    assert not promote(plant.pk, candidate)
    assert prepare_due_calibrations(NOW + timedelta(days=1)) == []
    plant.refresh_from_db()
    data = automation_status(plant)
    assert not data["authorized"]
    assert data["last_attempt"]["reason"] == "authorization_revoked"
    assert not CalibrationActivation.objects.exists()


def test_daily_catchup_is_bounded_idempotent_and_survives_missed_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )
    monkeypatch.setattr(timezone, "now", lambda: NOW + timedelta(days=4))
    start, _ = local_day_bounds(date(2026, 11, 13), system.region.timezone)
    assert prepare_due_calibrations(start) == []
    plant.refresh_from_db()
    expected, _ = local_day_bounds(date(2026, 11, 14), system.region.timezone)
    assert plant.next_calibration_check_at == expected
    assert prepare_due_calibrations(start + timedelta(minutes=1)) == []
    with pytest.raises(ValidationError, match="aware timestamp"):
        prepare_due_calibrations(datetime(2026, 11, 13))


def test_new_administrator_must_explicitly_reauthorize_existing_automation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    scope = (system.tenant.pk, system.region.pk, plant.pk)
    set_calibration_automation(system.member, *scope, enabled=True)
    replacement = User.objects.create_user(username="new-administrator")
    Membership.objects.create(user=replacement, tenant=system.tenant)
    reassign_workspace_admin(
        system.admin, system.tenant.pk, replacement.pk, expected_admin_id=system.member.pk
    )
    plant.refresh_from_db()
    assert not automation_status(plant)["authorized"]
    assert not plant.automatic_calibration_enabled
    assert plant.automatic_calibration_enabled_by_id is None
    plant = set_calibration_automation(replacement, *scope, enabled=True)
    assert automation_status(plant)["authorized"]
    assert plant.automatic_calibration_enabled_by_id == replacement.pk


def test_returning_administrator_cannot_revive_revoked_automation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    scope = (system.tenant.pk, system.region.pk, plant.pk)
    set_calibration_automation(system.member, *scope, enabled=True)
    other = Tenant.objects.create(name="Unaffected")
    Membership.objects.create(user=system.member, tenant=other, role=WorkspaceRole.ADMIN)
    other_region = Region.objects.create(
        tenant=other, name="Other site", latitude=41, longitude=30, timezone="UTC"
    )
    other_plant = Plant.objects.create(
        region=other_region,
        name="Other PV",
        automatic_calibration_enabled=True,
        automatic_calibration_enabled_by=system.member,
        next_calibration_check_at=NOW,
    )
    replacement = User.objects.create_user(username="temporary-administrator")
    Membership.objects.create(user=replacement, tenant=system.tenant)
    reassign_workspace_admin(
        system.admin, system.tenant.pk, replacement.pk, expected_admin_id=system.member.pk
    )
    reassign_workspace_admin(
        system.admin, system.tenant.pk, system.member.pk, expected_admin_id=replacement.pk
    )
    plant.refresh_from_db()
    assert not plant.automatic_calibration_enabled
    assert plant.automatic_calibration_enabled_by_id is None
    assert plant.next_calibration_check_at is None
    assert not automation_status(plant)["authorized"]
    assert (
        PlantEvent.objects.get(plant=plant, action="calibration_automation_disabled").actor_id
        == system.admin.pk
    )
    other_plant.refresh_from_db()
    assert other_plant.automatic_calibration_enabled
    assert other_plant.automatic_calibration_enabled_by_id == system.member.pk
    assert other_plant.next_calibration_check_at == NOW
    plant = set_calibration_automation(system.member, *scope, enabled=True)
    assert automation_status(plant)["authorized"]


def test_automation_endpoint_scopes_read_and_write_to_the_plant_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    other = Tenant.objects.create(name="Other")
    Membership.objects.create(user=system.member, tenant=other, role=WorkspaceRole.ADMIN)
    client = Client()
    client.force_login(system.member)
    wrong = (
        f"/api/tenants/{other.pk}/regions/{system.region.pk}/plants/"
        f"{plant.pk}/calibration/automation/"
    )
    assert client.get(wrong).status_code == 404
    assert (
        client.patch(wrong, {"enabled": True}, content_type="application/json").status_code == 404
    )
    Membership.objects.filter(user=system.member, tenant=other).delete()
    assert client.get(wrong).status_code == 403


def test_offline_fit_publication_automatically_promotes_without_external_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )
    import_fit_history(system, plant)
    run = CalibrationRun.objects.get()
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.assessment == "evaluated"
    assert CalibrationActivation.objects.get().calibration_id == run.pk
    assert not run_calibration(run.pk, NOW)
    assert CalibrationRun.objects.count() == 1


def test_concurrent_daily_dispatchers_advance_the_due_slot_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )
    unpublished_history(system, plant)
    barrier = Barrier(2)

    def dispatch_due() -> list[int]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return prepare_due_calibrations(NOW + timedelta(days=1))
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: dispatch_due(), range(2)))
    assert sorted(identifier for result in results for identifier in result) == [
        CalibrationRun.objects.get().pk
    ]
    plant.refresh_from_db()
    assert plant.next_calibration_check_at is not None
    assert plant.next_calibration_check_at > NOW + timedelta(days=1)


@pytest.mark.parametrize("mutation", ["disable", "trash_region", "purge_region"])
def test_due_scan_rechecks_a_concurrent_disable_or_definition_removal(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    system, plant = make_plant(monkeypatch)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    set_calibration_automation(*scope, enabled=True)
    before_lock, proceed = Event(), Event()
    original = Tenant.objects.select_for_update

    def synchronized_lock(*args: Any, **kwargs: Any) -> Any:
        if current_thread().name.startswith("daily-scan"):
            before_lock.set()
            assert proceed.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(Tenant.objects, "select_for_update", synchronized_lock)

    def dispatch_due() -> list[int]:
        close_old_connections()
        try:
            return prepare_due_calibrations(NOW + timedelta(days=1))
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="daily-scan") as executor:
        future = executor.submit(dispatch_due)
        try:
            assert before_lock.wait(timeout=10)
            if mutation == "disable":
                set_calibration_automation(*scope, enabled=False)
            else:
                trash_region(system.member, system.tenant.pk, system.region.pk)
            if mutation.startswith("purge"):
                purge_expired(NOW + timedelta(days=31))
        finally:
            proceed.set()
        assert future.result(timeout=10) == []
    assert not CalibrationRun.objects.exists()


def test_auto_promotion_rejects_stale_incumbent_and_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    set_calibration_automation(*scope, enabled=True)
    candidate = evaluated_run(system, plant)
    reset_calibration(*scope)
    assert not promote(plant.pk, candidate)
    set_calibration_automation(*scope, enabled=True)
    assert not promote(plant.pk, candidate)
    assert "changed" in PlantEvent.objects.latest("pk").details["reason"]
    revise_plant(*scope, PlantParameters(name="Changed", dc_capacity_mw=140))
    set_calibration_automation(*scope, enabled=True)
    assert not promote(plant.pk, candidate)
    assert PlantEvent.objects.latest("pk").details["reason"] == "configuration_changed"


def test_automation_enable_requires_approval_but_disable_remains_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    Region.objects.filter(pk=system.region.pk).update(status="suspended")
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    with pytest.raises(DomainError) as refusal:
        set_calibration_automation(*scope, enabled=True)
    assert refusal.value.code == "configuration_changed"
    assert not set_calibration_automation(*scope, enabled=False).automatic_calibration_enabled
    Plant.objects.filter(pk=plant.pk).update(current_configuration=None)
    with pytest.raises(DomainError) as refusal:
        set_calibration_automation(*scope, enabled=True)
    assert refusal.value.code == "configuration_unavailable"


def test_malformed_automatic_evaluation_never_activates_a_factor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )
    candidate = evaluated_run(system, plant)
    original = candidate.result.copy()
    for patch in (
        {"valid_days": "30"},
        {"valid_days": True},
        {"holdout_days": 6},
        {"holdout_day_dates": None},
        {"holdout_day_dates": [42]},
        {"holdout_day_dates": ["2026-11-09"] * 7},
        {"incumbent": None},
        {"candidate": "invalid"},
        {"candidate": {"interval_mae_mw": True}},
        {"candidate": {"interval_mae_mw": float("nan")}},
        {"candidate": {"interval_mae_mw": -1}},
        {"candidate": {"interval_mae_mw": "0.1"}},
    ):
        candidate.result = original | patch
        assert not promote(plant.pk, candidate)
        assert not CalibrationActivation.objects.exists()
    candidate.result = original
    candidate.window_end = date(2026, 10, 29)
    assert not promote(plant.pk, candidate)
    candidate.window_end = date(2026, 11, 9)
    candidate.result["holdout_day_dates"] = [
        (date(2026, 11, 3) + timedelta(days=offset)).isoformat() for offset in range(7)
    ]
    assert not promote(plant.pk, candidate)


def test_existing_legacy_activation_uses_its_window_end_for_new_holdout_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    initial = ready_run(system, plant)
    initial.window_start, initial.window_end = date(2026, 10, 1), date(2026, 10, 23)
    initial.save()
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, initial.pk)
    candidate = evaluated_run(system, plant)
    candidate.result["candidate"] = {"interval_mae_mw": 0.99, "daily_mae_mwh": 0.99}
    candidate.save()
    assert promote(plant.pk, candidate)
    assert CalibrationActivation.objects.latest("pk").calibration_id == candidate.pk


def test_enable_waits_for_pending_work_and_handles_explicit_baseline_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    candidate = evaluated_run(system, plant)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    baseline = reset_calibration(*scope)
    candidate.comparison_activation = baseline
    candidate.save()
    pending = CalibrationRun.objects.create(
        plant=plant,
        configuration=candidate.configuration,
        binding=system.binding,
        requested_by=system.member,
        window_start=candidate.window_start,
        window_end=candidate.window_end,
        cutoff_at=NOW,
        created_at=NOW - timedelta(minutes=1),
    )
    CalibrationRun.objects.filter(pk=pending.pk).update(created_at=NOW - timedelta(minutes=1))
    set_calibration_automation(*scope, enabled=True)
    assert CalibrationActivation.objects.latest("pk").pk == baseline.pk
    pending.status, pending.completed_at = "failed", NOW
    pending.save(update_fields=("status", "completed_at"))
    set_calibration_automation(*scope, enabled=True)
    assert CalibrationActivation.objects.latest("pk").calibration_id == candidate.pk


@pytest.mark.parametrize("limit", [True, 0, 1001])
def test_daily_scan_rejects_invalid_batch_limits(limit: int) -> None:
    with pytest.raises(ValidationError, match="limit"):
        prepare_due_calibrations(NOW, limit)


@pytest.mark.parametrize("failure", ["claim", "fit"])
def test_terminal_fit_failures_keep_baseline_and_surface_the_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from pyracast.plants import training

    system, plant = make_plant(monkeypatch)
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )
    run = evaluated_run(system, plant)
    run.status, run.attempts = "pending", 3 if failure == "claim" else 2
    run.save()

    def invalid_fit(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("Invalid fitting data")

    monkeypatch.setattr(training, "fit_calibration", invalid_fit)
    assert not run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "failed"
    plant.refresh_from_db()
    assert automation_status(plant)["last_attempt"]["reason"] == "candidate_failed"
    assert not CalibrationActivation.objects.exists()
