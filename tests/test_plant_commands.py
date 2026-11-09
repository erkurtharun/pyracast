from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    ProductionObservation,
)
from tests.test_domain import make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_operator_workflow_and_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    system = make_system(approved=True)
    monkeypatch.chdir(tmp_path)
    out = StringIO()
    call_command(
        "configure_plant",
        username="member",
        region_id=system.region.pk,
        name="Plant",
        dc_capacity_mw=1.0,
        stdout=out,
    )
    plant = Plant.objects.get()
    assert f"Plant {plant.pk}" in out.getvalue()
    assert plant.current_configuration_id is not None
    call_command(
        "configure_plant",
        username="member",
        region_id=system.region.pk,
        plant_id=plant.pk,
        name="Revised",
        dc_capacity_mw=2.0,
        stdout=out,
    )
    plant.refresh_from_db()
    config_id = plant.current_configuration_id
    assert config_id is not None
    csv_path = tmp_path / "meter.csv"
    csv_path.write_text(
        "\ufeffstarts_at,ends_at,energy,source\n2020-01-01T09:00:00Z,2020-01-01T09:30:00Z,500,meter\n",
        encoding="utf-8",
    )
    params = dict(
        username="member",
        plant_id=plant.pk,
        configuration_id=config_id,
        unit="kWh",
        file=csv_path,
        stdout=out,
    )
    call_command("import_production", **params)
    call_command("import_production", **params)
    assert ProductionObservation.objects.get().energy_mwh == 0.5
    assert "existing 1" in out.getvalue()
    csv_path.write_text(
        "starts_at,ends_at,energy\n2020-01-01T10:00:00Z,2020-01-01T10:30:00Z,5\n2020-01-01T09:00:00Z,2020-01-01T09:30:00Z,999\n",
        encoding="utf-8",
    )
    with pytest.raises(CommandError):
        call_command("import_production", **params)
    assert ProductionObservation.objects.count() == 1
    end = timezone.now().date() - timedelta(days=2)
    dates = dict(start_date=(end - timedelta(days=6)).isoformat(), end_date=end.isoformat())
    with pytest.raises(CommandError):
        call_command(
            "calibrate_plant",
            username="member",
            plant_id=plant.pk,
            configuration_id=config_id,
            start_date="invalid",
            end_date=end.isoformat(),
        )
    call_command(
        "calibrate_plant",
        username="member",
        plant_id=plant.pk,
        configuration_id=config_id,
        stdout=out,
        **dates,
    )
    assert CalibrationRun.objects.get().status == "pending"
    call_command(
        "calibrate_plant",
        username="member",
        plant_id=plant.pk,
        configuration_id=config_id,
        run=True,
        stdout=out,
        **dates,
    )
    assert CalibrationRun.objects.get().status == "insufficient"
    assert not CalibrationActivation.objects.exists()
    with pytest.raises(CommandError):
        call_command(
            "activate_calibration",
            username="member",
            plant_id=plant.pk,
            calibration_id=CalibrationRun.objects.get().pk,
        )
    call_command(
        "activate_calibration", username="member", plant_id=plant.pk, baseline=True, stdout=out
    )
    assert CalibrationActivation.objects.get().calibration_id is None
    User.objects.create_user(username="intruder", is_superuser=True)
    with pytest.raises(CommandError):
        call_command(
            "configure_plant",
            username="intruder",
            region_id=system.region.pk,
            name="No",
            dc_capacity_mw=1.0,
        )
    with pytest.raises(CommandError):
        call_command("activate_calibration", username="intruder", plant_id=plant.pk, baseline=True)


def test_csv_path_and_header_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    system = make_system(approved=True)
    call_command(
        "configure_plant",
        username="member",
        region_id=system.region.pk,
        name="Plant",
        dc_capacity_mw=1.0,
    )
    plant = Plant.objects.get()
    monkeypatch.chdir(tmp_path)
    params = dict(
        username="member",
        plant_id=plant.pk,
        configuration_id=plant.current_configuration_id,
        unit="MWh",
    )
    outside = tmp_path.parent / f"{tmp_path.name}.csv"
    outside.write_text("starts_at,ends_at,energy\n", encoding="utf-8")
    with pytest.raises(CommandError):
        call_command("import_production", file=outside, **params)
    path = tmp_path / "bad.csv"
    for content in (
        "starts_at,ends_at,energy,energy\n",
        "starts_at,ends_at,energy,unknown\n",
        "starts_at,ends_at,energy\n2020-01-01T09:00:00,2020-01-01T09:30:00,1\n",
        "starts_at,ends_at,energy\n2020-01-01T09:00:00Z,2020-01-01T09:30:00Z,nan\n",
    ):
        path.write_text(content, encoding="utf-8")
        with pytest.raises(CommandError):
            call_command("import_production", file=path, **params)
    assert not ProductionObservation.objects.exists()
    path.write_text("x" * (2 * 1024 * 1024 + 1), encoding="utf-8")
    with pytest.raises(CommandError):
        call_command("import_production", file=path, **params)
    path.write_text(
        "starts_at,ends_at,energy\n" + "2020-01-01T09:00:00Z,2020-01-01T09:30:00Z,1\n" * 2001,
        encoding="utf-8",
    )
    with pytest.raises(CommandError):
        call_command("import_production", file=path, **params)
