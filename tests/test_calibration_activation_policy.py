from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import Client
from psycopg.errors import LockNotAvailable

from pyracast.accounts.models import Membership
from pyracast.plants.activation import activate_calibration, reset_calibration
from pyracast.plants.automation import (
    automation_status,
    prepare_due_calibrations,
    set_calibration_automation,
)
from pyracast.plants.management.commands import activate_calibration as command
from pyracast.plants.models import CalibrationRun, Plant, PlantEvent
from pyracast.plants.operations import revise_plant
from pyracast.plants.parameters import PlantParameters
from tests.support.calibration_automation import evaluated_run, promote
from tests.support.domain import NOW, System
from tests.support.plant_operations import make_plant, ready_run, request_run

pytestmark = pytest.mark.django_db(transaction=True)


def plant_path(system: System, plant: Plant) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/"


def two_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[System, Plant, CalibrationRun, CalibrationRun]:
    system, plant = make_plant(monkeypatch)
    active = ready_run(system, plant)
    stale = evaluated_run(system, plant)
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, active.pk)
    return system, plant, active, stale


def skip_reasons(plant: Plant) -> list[str]:
    return list(
        PlantEvent.objects.filter(plant=plant, action="calibration_automatic_skipped")
        .order_by("pk")
        .values_list("details__reason", flat=True)
    )


def test_the_run_detail_shows_the_blocker_the_activation_refuses_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, active, stale = two_candidates(monkeypatch)
    client = Client()
    client.force_login(system.member)
    runs = f"{plant_path(system, plant)}calibrations/"

    refused = client.post(f"{runs}{stale.pk}/activate/", {}, content_type="application/json")
    detail = client.get(f"{runs}{stale.pk}/").json()
    current = client.get(f"{runs}{active.pk}/").json()
    listed = {row["id"]: row for row in client.get(runs).json()["results"]}

    assert refused.status_code == 400
    assert refused.json()["code"] == "incumbent_changed"
    assert (detail["selection_state"], detail["activation_blocker"]) == (
        "candidate",
        "incumbent_changed",
    )
    assert (current["selection_state"], current["activation_blocker"]) == ("active", None)
    assert listed[stale.pk]["selection_state"] == "candidate"
    assert listed[active.pk]["selection_state"] == "active"
    assert "activation_blocker" not in listed[stale.pk]


def test_runs_of_a_revised_configuration_read_as_previous_and_open_runs_as_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, active, _ = two_candidates(monkeypatch)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    revise_plant(*scope, PlantParameters(name="Changed", dc_capacity_mw=140))
    plant.refresh_from_db()
    open_run = request_run(system, plant)
    client = Client()
    client.force_login(system.member)
    runs = f"{plant_path(system, plant)}calibrations/"

    previous = client.get(f"{runs}{active.pk}/").json()
    pending = client.get(f"{runs}{open_run.pk}/").json()
    listed = {row["id"]: row for row in client.get(runs).json()["results"]}

    assert (previous["selection_state"], previous["activation_blocker"]) == (
        "previous_configuration",
        "configuration_changed",
    )
    assert (pending["selection_state"], pending["activation_blocker"]) == (
        "not_ready",
        "calibration_not_activatable",
    )
    assert listed[open_run.pk]["selection_state"] == "not_ready"
    assert automation_status(plant)["current_calibration"] is None


def test_activating_a_previous_configuration_run_refuses_with_the_blocker_the_detail_shows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, active, _ = two_candidates(monkeypatch)
    revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        PlantParameters(name="Changed", dc_capacity_mw=140),
    )
    client = Client()
    client.force_login(system.member)
    runs = f"{plant_path(system, plant)}calibrations/"

    refused = client.post(f"{runs}{active.pk}/activate/", {}, content_type="application/json")

    assert (refused.status_code, refused.json()["code"]) == (400, "configuration_changed")
    assert client.get(f"{runs}{active.pk}/").json()["activation_blocker"] == "configuration_changed"
    with pytest.raises(CommandError, match=r"^Activation refused: configuration_changed\.$"):
        call_command(
            "activate_calibration", username="member", plant_id=plant.pk, calibration_id=active.pk
        )


def test_a_baseline_reset_reads_as_the_effective_current_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _, _ = two_candidates(monkeypatch)

    reset_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk)
    plant.refresh_from_db()

    current = automation_status(plant)["current_calibration"]
    assert {key: current[key] for key in ("calibration_id", "factor", "origin", "effective")} == {
        "calibration_id": None,
        "factor": None,
        "origin": "manual",
        "effective": True,
    }
    assert automation_status(plant)["disabled_cause"] == "reset"


def test_an_automatic_promotion_records_the_blocker_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _, plant, _, stale = two_candidates(monkeypatch)

    assert not promote(plant.pk, stale)

    assert skip_reasons(plant) == ["incumbent_changed"]


def test_the_operator_command_names_the_blocker(monkeypatch: pytest.MonkeyPatch) -> None:
    _, plant, _, stale = two_candidates(monkeypatch)

    with pytest.raises(CommandError, match=r"^Activation refused: incumbent_changed\.$"):
        call_command(
            "activate_calibration", username="member", plant_id=plant.pk, calibration_id=stale.pk
        )


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"username": "nobody"}, "User nobody not found."),
        ({"plant_id": 0}, "Plant 0 not found."),
        ({"username": "outsider"}, "User outsider may not change plant"),
        ({"calibration_id": 0}, r"^Calibration 0 not found\.$"),
    ],
)
def test_the_operator_command_names_each_refusal(
    monkeypatch: pytest.MonkeyPatch, options: dict[str, object], message: str
) -> None:
    _, plant, active, _ = two_candidates(monkeypatch)
    User.objects.create_user(username="outsider")
    arguments = {"username": "member", "plant_id": plant.pk, "calibration_id": active.pk}

    with pytest.raises(CommandError, match=message):
        call_command("activate_calibration", **(arguments | options))


def test_the_operator_command_reports_a_busy_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    _, plant, _, _ = two_candidates(monkeypatch)

    def held(*args: object) -> None:
        error = OperationalError("lock")
        error.__cause__ = LockNotAvailable("lock")
        raise error

    monkeypatch.setattr(command, "reset_calibration", held)

    with pytest.raises(CommandError, match=r"^Workspace busy; retry\.$"):
        call_command("activate_calibration", username="member", plant_id=plant.pk, baseline=True)


def test_a_paused_automation_records_its_revoked_authorization_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )
    Membership.objects.filter(user=system.member).delete()

    prepare_due_calibrations(NOW + timedelta(days=1))
    prepare_due_calibrations(NOW + timedelta(days=2))

    assert skip_reasons(plant) == ["authorization_revoked"]


def test_re_enabling_automation_does_not_reevaluate_the_active_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    run = ready_run(system, plant)
    activate_calibration(*scope, run.pk)
    set_calibration_automation(*scope, enabled=False)

    set_calibration_automation(*scope, enabled=True)

    assert skip_reasons(plant) == []


def test_automation_status_reads_the_current_selection_beside_the_last_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    run = evaluated_run(system, plant)
    set_calibration_automation(*scope, enabled=True)
    assert promote(plant.pk, run)
    later = ready_run(system, plant)
    assert not promote(plant.pk, later)
    plant.refresh_from_db()

    status = automation_status(plant)

    assert status["configuration_current"] is True
    assert status["last_attempt"]["status"] == "skipped"
    current = status["current_calibration"]
    assert {key: current[key] for key in ("calibration_id", "factor", "origin", "actor_id")} == {
        "calibration_id": run.pk,
        "factor": run.factor,
        "origin": "automatic",
        "actor_id": None,
    }
    assert current["effective"] is True
    set_calibration_automation(*scope, enabled=False)
    plant.refresh_from_db()
    assert automation_status(plant)["disabled_cause"] == "toggle"
