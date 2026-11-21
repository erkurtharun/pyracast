import logging
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from django.core.management import CommandError, call_command
from django.db import OperationalError

from pyracast.background.failures import ItemsFailedError, PhaseOutcome
from pyracast.forecasts import runtime, scheduler
from pyracast.forecasts.scheduler import CollectionSchedulingError
from pyracast.orchestration import dispatcher as command
from pyracast.shutdown import ShutdownRequest
from tests.support.domain import make_system
from tests.support.kinds import stub_actors


@pytest.fixture(autouse=True)
def isolated_monitoring(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    record = MagicMock()
    monkeypatch.setattr(command, "record_dispatch_phase", record)
    return record


@pytest.mark.parametrize(
    "failed_phase",
    ["purge_expired", "dispatch_collections", "dispatch_calibrations", "dispatch_epias"],
)
def test_dispatch_failure_does_not_suppress_email_and_one_shot_reports_failure(
    failed_phase: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    broker = MagicMock()
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    stub_actors(monkeypatch)
    monkeypatch.setattr(command, "dispatch_revalidations", MagicMock(return_value=0))
    monkeypatch.setattr(command, "purge_finished_mail", MagicMock())
    operations = {}
    for name in (
        "purge_expired",
        "dispatch_collections",
        "dispatch_calibrations",
        "dispatch_notifications",
        "dispatch_epias",
    ):
        operations[name] = MagicMock()
        monkeypatch.setattr(command, name, operations[name])
    operations["dispatch_collections"].return_value = PhaseOutcome()
    monkeypatch.setattr(command, "sweep_accounts", MagicMock())
    monkeypatch.setattr(command, "warn_before_purge", MagicMock())
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
    stub_actors(monkeypatch)
    monkeypatch.setattr(command, "dispatch_revalidations", MagicMock(return_value=0))
    notifications = MagicMock()
    monkeypatch.setattr(command, "dispatch_notifications", notifications)
    for name in (
        "purge_expired",
        "sweep_accounts",
        "warn_before_purge",
        "purge_finished_mail",
        "publish_outbound_mail",
        "dispatch_calibrations",
        "dispatch_epias",
    ):
        monkeypatch.setattr(command, name, MagicMock())
    monkeypatch.setattr(command, "dispatch_collections", MagicMock(return_value=PhaseOutcome()))
    monkeypatch.setattr(command, "record_dispatch_phase", MagicMock(side_effect=ConnectionError))
    with pytest.raises(CommandError, match="monitoring"):
        call_command("dispatch")
    notifications.assert_called_once()


def test_dispatch_loop_recovers_after_a_failed_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = MagicMock()
    monkeypatch.setattr(command, "make_broker", lambda: broker)
    stub_actors(monkeypatch)
    monkeypatch.setattr(command, "dispatch_revalidations", MagicMock(return_value=0))
    for name in (
        "purge_expired",
        "sweep_accounts",
        "warn_before_purge",
        "purge_finished_mail",
        "publish_outbound_mail",
        "dispatch_calibrations",
        "dispatch_notifications",
    ):
        monkeypatch.setattr(command, name, MagicMock())
    collection = MagicMock(side_effect=[ConnectionError("private-broker-url"), PhaseOutcome()])
    monkeypatch.setattr(command, "dispatch_collections", collection)
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


def mock_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command, "make_broker", MagicMock())
    stub_actors(monkeypatch)
    monkeypatch.setattr(command, "dispatch_revalidations", MagicMock(return_value=0))


def recorded_phases(record: MagicMock) -> dict[str, tuple[str, int | None, int]]:
    return {
        call.args[0]: (call.args[2], call.kwargs["published"], call.kwargs["duration_ms"])
        for call in record.call_args_list
    }


def test_phase_records_duration_and_published_count(
    monkeypatch: pytest.MonkeyPatch,
    pyracast_logs: pytest.LogCaptureFixture,
    isolated_monitoring: MagicMock,
) -> None:
    mock_runtime(monkeypatch)
    monkeypatch.setattr(command, "dispatch_notifications", MagicMock(return_value=3))
    monkeypatch.setattr(
        command, "dispatch_collections", MagicMock(return_value=PhaseOutcome(published=2))
    )
    monkeypatch.setattr(command, "dispatch_calibrations", MagicMock(return_value=1))
    monkeypatch.setattr(command, "purge_expired", MagicMock(return_value={"plants": 4}))
    monkeypatch.setattr(command, "sweep_accounts", MagicMock())
    monkeypatch.setattr(command, "warn_before_purge", MagicMock())
    monkeypatch.setattr(command, "purge_finished_mail", MagicMock())
    monkeypatch.setattr(command, "dispatch_epias", MagicMock(return_value=0))
    call_command("dispatch")
    phases = recorded_phases(isolated_monitoring)
    assert {name: values[:2] for name, values in phases.items()} == {
        "notifications": ("", 3),
        "collection": ("", 2),
        "calibration": ("", 1),
        "retention": ("", None),
        "metering": ("", 0),
    }
    assert all(values[2] >= 0 for values in phases.values())
    finished = [
        record.getMessage()
        for record in pyracast_logs.records
        if record.getMessage().startswith("dispatch_phase_finished")
    ]
    assert len(finished) == 5
    assert re.fullmatch(
        r"dispatch_phase_finished phase=collection duration_ms=\d+ published=2 error=", finished[1]
    )


def test_the_mail_purge_runs_beside_retention_steps_whose_items_failed(
    monkeypatch: pytest.MonkeyPatch, isolated_monitoring: MagicMock
) -> None:
    mock_runtime(monkeypatch)
    for name in ("dispatch_notifications", "dispatch_calibrations", "dispatch_epias"):
        monkeypatch.setattr(command, name, MagicMock(return_value=0))
    monkeypatch.setattr(command, "dispatch_collections", MagicMock(return_value=PhaseOutcome()))
    monkeypatch.setattr(command, "purge_expired", MagicMock(side_effect=ItemsFailedError(1)))
    monkeypatch.setattr(command, "sweep_accounts", MagicMock(side_effect=ItemsFailedError(2)))
    monkeypatch.setattr(command, "warn_before_purge", MagicMock())
    purge_mail = MagicMock()
    monkeypatch.setattr(command, "purge_finished_mail", purge_mail)

    with pytest.raises(CommandError, match="retention"):
        call_command("dispatch")

    purge_mail.assert_called_once()
    assert recorded_phases(isolated_monitoring)["retention"][:2] == ("items_failed:3", None)


@pytest.mark.django_db(transaction=True)
def test_collection_scheduling_failure_records_region_count_and_published_jobs(
    monkeypatch: pytest.MonkeyPatch,
    pyracast_logs: pytest.LogCaptureFixture,
    isolated_monitoring: MagicMock,
) -> None:
    make_system(approved=True)
    mock_runtime(monkeypatch)

    def contended(*args: object) -> list[int]:
        raise OperationalError("private lock detail")

    monkeypatch.setattr(scheduler, "_enqueue_region", contended)
    monkeypatch.setattr(runtime, "pending_job_ids", MagicMock(return_value=[11, 19]))
    for name in ("dispatch_notifications", "dispatch_calibrations", "dispatch_epias"):
        monkeypatch.setattr(command, name, MagicMock(return_value=0))
    monkeypatch.setattr(command, "purge_expired", MagicMock(return_value={}))
    monkeypatch.setattr(command, "purge_finished_mail", MagicMock())
    with pytest.raises(CommandError, match="collection"):
        call_command("dispatch")
    assert recorded_phases(isolated_monitoring)["collection"][:2] == (
        "collection_regions_failed:1",
        2,
    )
    assert "private lock detail" not in pyracast_logs.text


@pytest.mark.parametrize(
    ("scheduling", "error"),
    [
        (CollectionSchedulingError([4, 8]), "collection_regions_failed:2"),
        (None, "ConnectionError"),
    ],
)
def test_collection_keeps_its_first_cause_when_publishing_fails(
    monkeypatch: pytest.MonkeyPatch,
    pyracast_logs: pytest.LogCaptureFixture,
    scheduling: Exception | None,
    error: str,
) -> None:
    monkeypatch.setattr(runtime, "enqueue_due", MagicMock(side_effect=scheduling))
    monkeypatch.setattr(runtime, "pending_job_ids", MagicMock(return_value=[11, 19]))
    monkeypatch.setattr(runtime, "close_old_connections", MagicMock())
    actor = MagicMock()
    actor.send.side_effect = [None, ConnectionError("private-broker-url")]
    assert runtime.dispatch_collections(actor) == PhaseOutcome(published=1, error=error)
    assert "collection_publish_failed published=1 error_type=ConnectionError" in pyracast_logs.text
    assert "private-broker-url" not in pyracast_logs.text


def test_mail_is_published_between_dispatch_cycles_and_a_failure_does_not_end_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(command, "MAIL_TICK_SECONDS", 0.001)
    calls: list[int] = []

    def published() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("private-broker-url")
        return 1

    shutdown = ShutdownRequest(tmp_path / "stop")

    assert not command._wait_publishing_mail(shutdown, 0.05, published)

    assert len(calls) >= 2
    assert "mail_publication_failed" in pyracast_logs.text
    assert "private-broker-url" not in pyracast_logs.text
    (tmp_path / "stop").touch()
    assert command._wait_publishing_mail(shutdown, 60, published)
