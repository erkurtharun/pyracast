from pathlib import Path
from unittest.mock import MagicMock

import pytest
from django.core.management import call_command
from dramatiq.brokers.rabbitmq import RabbitmqBroker

from pyracast.background.failures import PhaseOutcome
from pyracast.forecasts.management.commands import dispatch as command
from tests.support.rabbitmq import install_fake_rabbitmq


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
        command, "dispatch_collections", MagicMock(side_effect=[ConnectionError, PhaseOutcome()])
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


def test_the_heartbeat_is_renewed_only_after_the_broker_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rabbitmq = install_fake_rabbitmq(monkeypatch)
    monkeypatch.setattr(
        command,
        "make_broker",
        lambda: RabbitmqBroker(url="amqp://broker.invalid/", confirm_delivery=True),
    )
    monkeypatch.setattr(command, "record_dispatch_phase", MagicMock())
    for name in (
        "purge_expired",
        "sweep_accounts",
        "purge_finished_mail",
        "publish_outbound_mail",
        "dispatch_calibrations",
        "dispatch_notifications",
    ):
        monkeypatch.setattr(command, name, MagicMock(return_value=0))
    monkeypatch.setattr(command, "dispatch_collections", MagicMock(return_value=PhaseOutcome()))
    heartbeat = tmp_path / "dispatcher-heartbeat"
    stop_file = tmp_path / "stop"
    # Per round: whether the broker is reachable, and whether a phase lost the connection.
    rounds = [(True, False), (True, True), (False, False), (False, False), (True, False)]
    renewed: list[bool] = []

    def last_phase(_actor: object) -> None:
        renewed.append(heartbeat.exists())
        heartbeat.unlink(missing_ok=True)
        if len(renewed) == len(rounds):
            stop_file.touch()
            return
        rabbitmq.reachable, lost = rounds[len(renewed)]
        if lost or not rabbitmq.reachable:
            rabbitmq.drop()

    monkeypatch.setattr(command, "dispatch_epias", last_phase)
    call_command(
        "dispatch",
        loop=True,
        poll_seconds=0.001,
        stop_file=stop_file,
        heartbeat_file=heartbeat,
    )

    assert renewed == [True, True, False, False, True]
