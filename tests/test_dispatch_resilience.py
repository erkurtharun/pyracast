import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from django.core.management import CommandError, call_command

from pyracast.forecasts.management.commands import dispatch as command


@pytest.fixture(autouse=True)
def isolated_monitoring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command, "record_dispatch_phase", MagicMock())


@pytest.mark.parametrize(
    "failed_phase", ["purge_expired", "dispatch", "dispatch_calibrations", "dispatch_epias"]
)
def test_dispatch_failure_does_not_suppress_email_and_one_shot_reports_failure(
    failed_phase: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    broker = MagicMock()
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    for factory in (
        "make_actor",
        "make_calibration_actor",
        "make_notification_actor",
        "make_epias_actor",
    ):
        monkeypatch.setattr(command, factory, MagicMock())
    operations = {}
    for name in (
        "purge_expired",
        "dispatch",
        "dispatch_calibrations",
        "dispatch_notifications",
        "dispatch_epias",
    ):
        operations[name] = MagicMock()
        monkeypatch.setattr(command, name, operations[name])
    operations[failed_phase].side_effect = RuntimeError("private-provider-response")
    monkeypatch.setattr(command, "logger", logging.getLogger("test.dispatch"), raising=False)

    with pytest.raises(CommandError, match="dispatch phase"):
        call_command("dispatch")

    operations["dispatch_notifications"].assert_called_once()
    assert all(operation.call_count == 1 for operation in operations.values())
    assert "private-provider-response" not in caplog.text
    assert "RuntimeError" in caplog.text
    broker.close.assert_called_once()


def test_dispatch_monitoring_failure_does_not_skip_other_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = MagicMock()
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    for factory in (
        "make_actor",
        "make_calibration_actor",
        "make_notification_actor",
        "make_epias_actor",
    ):
        monkeypatch.setattr(command, factory, MagicMock())
    notifications = MagicMock()
    monkeypatch.setattr(command, "dispatch_notifications", notifications)
    for name in ("purge_expired", "dispatch", "dispatch_calibrations", "dispatch_epias"):
        monkeypatch.setattr(command, name, MagicMock())
    monkeypatch.setattr(command, "record_dispatch_phase", MagicMock(side_effect=ConnectionError))
    with pytest.raises(CommandError, match="monitoring"):
        call_command("dispatch")
    notifications.assert_called_once()


def test_dispatch_loop_recovers_after_a_failed_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = MagicMock()
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    for factory in (
        "make_actor",
        "make_calibration_actor",
        "make_notification_actor",
        "make_epias_actor",
    ):
        monkeypatch.setattr(command, factory, MagicMock())
    for name in ("purge_expired", "dispatch_calibrations", "dispatch_notifications"):
        monkeypatch.setattr(command, name, MagicMock())
    collection = MagicMock(side_effect=[ConnectionError("private-broker-url"), None])
    monkeypatch.setattr(command, "dispatch", collection)
    stop_file = tmp_path / "stop"
    cycles = []

    def finish(_: object) -> None:
        cycles.append("finished")
        if len(cycles) == 2:
            stop_file.touch()

    monkeypatch.setattr(command, "dispatch_epias", finish)
    call_command("dispatch", loop=True, poll_seconds=0.001, stop_file=stop_file)
    assert cycles == ["finished", "finished"]
    assert collection.call_count == 2
    broker.close.assert_called_once()
