from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User

from pyracast.accounts import operations as accounts
from pyracast.accounts.invitations import accept_workspace_invitation, invitation_digest
from pyracast.accounts.models import Membership, Tenant, WorkspaceInvitation, WorkspaceRole
from pyracast.accounts.operations import reassign_workspace_admin, remove_workspace_member
from pyracast.metering import operations as metering
from pyracast.metering.models import EpiasConnection
from pyracast.plants import automation, preparation, training
from pyracast.plants.automation import prepare_due_calibrations, set_calibration_automation
from pyracast.plants.models import CalibrationActivation, CalibrationRun, Plant, PlantEvent
from tests.support.calibration_automation import import_fit_history
from tests.support.concurrency import committed_elsewhere, paused_in
from tests.support.domain import NOW, System
from tests.support.metering import connect, connected
from tests.support.plant_operations import make_plant, request_run

pytestmark = pytest.mark.django_db(transaction=True)

DUE = NOW + timedelta(days=1)


def automated(system: System, plant: Plant) -> None:
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )


def automatic_skips(plant: Plant) -> list[str]:
    return list(
        PlantEvent.objects.filter(plant=plant, action="calibration_automatic_skipped")
        .order_by("pk")
        .values_list("details__reason", flat=True)
    )


def checked_after_due(plant: Plant) -> bool:
    plant.refresh_from_db()
    return plant.next_calibration_check_at is not None and plant.next_calibration_check_at > DUE


def automation_step(system: System, plant: Plant, step: str) -> Callable[[], object]:
    if step == "due_check":
        return lambda: prepare_due_calibrations(DUE)
    run = request_run(system, plant)
    CalibrationRun.objects.filter(pk=run.pk).update(attempts=3)
    return lambda: training._claim(run.pk, NOW)


@pytest.mark.parametrize("change", ["connect", "disconnect"])
@pytest.mark.parametrize("step", ["due_check", "claim"])
def test_the_enablers_production_source_change_and_the_automation_cannot_wait_for_each_other(
    monkeypatch: pytest.MonkeyPatch, step: str, change: str
) -> None:
    system, plant, _linked = connected(monkeypatch)
    system.member.is_staff = system.member.is_superuser = True
    system.member.save(update_fields=("is_staff", "is_superuser"))
    automated(system, plant)
    if change == "connect":
        metering.disconnect_epias(plant.pk, actor=system.member)
    hold = automation_step(system, plant, step)

    def change_connection() -> bool:
        if change == "connect":
            return connect(plant, system.member).enabled
        return metering.disconnect_epias(plant.pk, actor=system.member)

    _held, changed = paused_in(monkeypatch, automation, "_authorized", hold, change_connection)

    assert changed is True
    assert EpiasConnection.objects.get(plant=plant).enabled is (change == "connect")
    if step == "due_check":
        assert checked_after_due(plant)
        assert automatic_skips(plant) == []
    else:
        assert automatic_skips(plant) == [f"candidate_{CalibrationRun.objects.get().status}"]


def test_the_enablers_invitation_acceptance_and_the_automation_cannot_wait_for_each_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    automated(system, plant)
    EmailAddress.objects.create(user=system.member, email="alias@example.com", verified=True)
    WorkspaceInvitation.objects.create(
        tenant=system.tenant,
        email="alias@example.com",
        token_hash=invitation_digest("alias-token"),
        created_by=system.member,
        expires_at=NOW + timedelta(days=7),
    )

    _held, membership = paused_in(
        monkeypatch,
        automation,
        "_authorized",
        lambda: prepare_due_calibrations(DUE),
        lambda: accept_workspace_invitation(system.member, "alias-token"),
    )

    assert (membership.user_id, membership.role) == (system.member.pk, WorkspaceRole.ADMIN)
    assert checked_after_due(plant)
    assert automatic_skips(plant) == []


@pytest.mark.parametrize("change", ["due_check", "production_source"])
def test_a_member_removal_by_a_person_and_that_persons_own_change_cannot_wait_for_each_other(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    # The removal holds the workspace and its regions and commits a row that names its actor.
    system, plant, _linked = connected(monkeypatch)
    automated(system, plant)
    viewer = User.objects.create(username="viewer")
    Membership.objects.create(user=viewer, tenant=system.tenant)
    actor = system.member if change == "due_check" else system.admin

    def own_change() -> object:
        if change == "due_check":
            return prepare_due_calibrations(DUE)
        return metering.disconnect_epias(plant.pk, actor=system.admin)

    removed, _changed = paused_in(
        monkeypatch,
        accounts,
        "record_security_event",
        lambda: remove_workspace_member(
            actor, system.tenant.pk, viewer.pk, platform=change == "production_source"
        ),
        own_change,
    )

    assert removed is True
    assert not Membership.objects.filter(user=viewer).exists()
    if change == "due_check":
        assert checked_after_due(plant)
    else:
        assert not EpiasConnection.objects.get(plant=plant).enabled


def passed_to(system: System, plant: Plant, successor: User) -> list[Callable[[], object]]:
    return [
        lambda: reassign_workspace_admin(
            system.admin, system.tenant.pk, successor.pk, expected_admin_id=system.member.pk
        ),
        lambda: set_calibration_automation(
            successor, system.tenant.pk, system.region.pk, plant.pk, enabled=True
        ),
    ]


def changed_at_the_workspace_lock(
    monkeypatch: pytest.MonkeyPatch, owner: object, step: str, *changes: Callable[[], object]
) -> None:
    # The changes commit on another connection when `owner.step` first locks a workspace, after
    # every read and lock it takes before that one.
    original = getattr(owner, step)
    locking = Tenant.objects.select_for_update
    pending: list[Callable[[], object]] = []

    def lock(*args: Any, **kwargs: Any) -> Any:
        due, pending[:] = list(pending), []
        for change in due:
            committed_elsewhere(change)
        return locking(*args, **kwargs)

    def stepped(*args: Any, **kwargs: Any) -> Any:
        pending.extend(changes)
        return original(*args, **kwargs)

    monkeypatch.setattr(Tenant.objects, "select_for_update", lock)
    monkeypatch.setattr(owner, step, stepped)


def successor_of(system: System) -> User:
    successor = User.objects.create(username="successor")
    Membership.objects.create(user=successor, tenant=system.tenant)
    return successor


def test_a_due_check_whose_enabler_changed_meanwhile_leaves_the_plant_due_for_the_next_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    automated(system, plant)
    successor = successor_of(system)
    changed_at_the_workspace_lock(
        monkeypatch, automation, "_prepare_due", *passed_to(system, plant, successor)
    )

    assert prepare_due_calibrations(DUE) == []

    plant.refresh_from_db()
    assert plant.automatic_calibration_enabled_by_id == successor.pk
    assert not checked_after_due(plant)
    assert automatic_skips(plant) == []
    assert prepare_due_calibrations(DUE) == []
    assert checked_after_due(plant)
    assert automatic_skips(plant) == []


def test_a_publication_whose_enabler_changed_meanwhile_leaves_the_candidate_to_its_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    automated(system, plant)
    import_fit_history(system, plant)
    run = CalibrationRun.objects.get()
    successor = successor_of(system)
    monkeypatch.setattr(preparation, "_prepare_after_commit", lambda *_args: None)
    changed_at_the_workspace_lock(
        monkeypatch, training, "_publish", *passed_to(system, plant, successor)
    )

    assert training.run_calibration(run.pk, NOW)

    run.refresh_from_db()
    plant.refresh_from_db()
    assert run.status == "ready"
    assert plant.automatic_calibration_enabled_by_id == successor.pk
    assert not CalibrationActivation.objects.exists()
    assert automatic_skips(plant) == []
    assert plant.calibration_preparation_due_at is not None
    assert preparation.prepare_requested_calibrations(NOW) == []
    assert CalibrationActivation.objects.get().calibration_id == run.pk
    assert automatic_skips(plant) == []
