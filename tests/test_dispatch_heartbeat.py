from pathlib import Path
from unittest.mock import MagicMock

import pytest
from django.core.management import call_command

from pyracast.forecasts.management.commands import dispatch as command
from pyracast.forecasts.monitoring import PhaseOutcome


def test_dispatcher_touches_its_heartbeat_before_every_cycle_even_after_a_failed_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command, "record_dispatch_phase", MagicMock())
    monkeypatch.setattr(command, "make_broker", MagicMock)
    for name in (
        "make_actor",
        "make_calibration_actor",
        "make_notification_actor",
        "make_outbound_mail_actor",
        "make_epias_actor",
        "purge_expired",
        "sweep_accounts",
        "purge_finished_mail",
        "publish_outbound_mail",
        "dispatch_calibrations",
        "dispatch_notifications",
    ):
        monkeypatch.setattr(command, name, MagicMock())
    monkeypatch.setattr(
        command, "dispatch", MagicMock(side_effect=[ConnectionError, PhaseOutcome()])
    )
    heartbeat = tmp_path / "dispatcher-heartbeat"
    stop_file = tmp_path / "stop"
    seen: list[bool] = []

    def last_phase(_actor: object) -> None:
        seen.append(heartbeat.exists())
        heartbeat.unlink(missing_ok=True)
        if len(seen) == 2:
            stop_file.touch()

    monkeypatch.setattr(command, "dispatch_epias", last_phase)
    call_command(
        "dispatch",
        loop=True,
        poll_seconds=0.001,
        stop_file=stop_file,
        heartbeat_file=heartbeat,
    )
    assert seen == [True, True]
