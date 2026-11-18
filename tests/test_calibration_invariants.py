import pytest
from django.db import IntegrityError, transaction

from pyracast.plants.automation import set_calibration_automation
from pyracast.plants.models import CalibrationRun, Plant
from tests.support.domain import NOW
from tests.support.invariants import CALIBRATION_RUN_RULE, PLANT_AUTOMATION_RULE, violations
from tests.support.plant_operations import make_plant, request_run

pytestmark = pytest.mark.django_db(transaction=True)


def _clean() -> tuple[int, int]:
    return (
        violations("plants_calibrationrun", CALIBRATION_RUN_RULE),
        violations("plants_plant", PLANT_AUTOMATION_RULE),
    )


@pytest.mark.parametrize("status", ["ready", "insufficient", "rejected", "failed", "superseded"])
def test_a_terminal_calibration_run_carries_its_completion_time(
    status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = request_run(*make_plant(monkeypatch))
    rows = CalibrationRun.objects.filter(pk=run.pk)

    with (
        pytest.raises(IntegrityError, match="calibrationrun_completed_at_chk"),
        transaction.atomic(),
    ):
        rows.update(status=status)

    rows.update(status=status, completed_at=NOW)
    assert _clean() == (0, 0)


def test_a_calibration_assessment_belongs_to_its_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = request_run(*make_plant(monkeypatch))
    rows = CalibrationRun.objects.filter(pk=run.pk)

    with (
        pytest.raises(IntegrityError, match="calibrationrun_assessment_chk"),
        transaction.atomic(),
    ):
        rows.update(assessment="excellent")

    for value in ("insufficient", "provisional", "evaluated", ""):
        rows.update(assessment=value)
    assert _clean() == (0, 0)


@pytest.mark.parametrize(
    "stored",
    [
        ("enabled",),
        ("enabled", "user"),
        ("enabled", "next_check"),
        ("user",),
        ("next_check",),
    ],
)
def test_plant_automation_is_enabled_with_its_user_and_next_check_or_holds_neither(
    stored: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    system, plant = make_plant(monkeypatch)
    rows = Plant.objects.filter(pk=plant.pk)
    values = {
        "automatic_calibration_enabled": "enabled" in stored,
        "automatic_calibration_enabled_by": system.member if "user" in stored else None,
        "next_calibration_check_at": NOW if "next_check" in stored else None,
    }

    with pytest.raises(IntegrityError, match="plant_automation_chk"), transaction.atomic():
        rows.update(**values)

    rows.update(
        automatic_calibration_enabled=True,
        automatic_calibration_enabled_by=system.member,
        next_calibration_check_at=NOW,
    )
    assert _clean() == (0, 0)


def test_automation_writers_store_the_whole_automation_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)

    enabled = set_calibration_automation(*scope, enabled=True)

    assert enabled.automatic_calibration_enabled_by_id == system.member.pk
    assert enabled.next_calibration_check_at is not None
    assert _clean() == (0, 0)
    disabled = set_calibration_automation(*scope, enabled=False)
    assert (
        disabled.automatic_calibration_enabled,
        disabled.automatic_calibration_enabled_by_id,
        disabled.next_calibration_check_at,
    ) == (False, None, None)
    assert _clean() == (0, 0)
