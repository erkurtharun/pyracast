import inspect
import math
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from django.conf import settings
from dramatiq.brokers.stub import StubBroker

from pyracast.background import deadlines
from pyracast.forecasts import jobs as collection_jobs
from pyracast.forecasts.management.commands import dispatch, worker
from pyracast.metering import jobs as metering_jobs
from pyracast.notifications import jobs as notification_jobs
from pyracast.plants import training
from pyracast.plants.training import pending_calibration_ids
from scripts import dev as launcher

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def compose_services() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))
    services: dict[str, dict[str, Any]] = document["services"]
    return services


def duration(value: str) -> timedelta:
    match = re.fullmatch(r"(\d+)s", value)
    assert match, value
    return timedelta(seconds=int(match[1]))


def registered_actors() -> dict[str, Any]:
    broker = StubBroker()
    worker.register_actors(broker)
    return dict(broker.actors)


def test_every_registered_actor_runs_under_its_bound() -> None:
    actors = registered_actors()

    assert set(actors) == set(deadlines.ACTORS)
    for name, actor in actors.items():
        bound = deadlines.ACTORS[name]
        expected = math.inf if bound.time_limit is None else bound.time_limit.total_seconds() * 1000
        assert actor.options["time_limit"] == expected, name


def test_mail_actors_are_never_interrupted_after_their_transmit_marker() -> None:
    assert deadlines.ACTORS["send_forecast_reminder"].time_limit is None
    assert registered_actors()["send_forecast_reminder"].options["time_limit"] == math.inf


def test_an_interrupted_actor_stops_before_its_lease_can_be_claimed_again() -> None:
    blocking_call = timedelta(
        seconds=max(
            deadlines.DATABASE_STATEMENT_SECONDS,
            deadlines.HTTP_CONNECT_SECONDS + deadlines.HTTP_READ_SECONDS,
            deadlines.MAIL_SOCKET_SECONDS,
        )
    )
    for name, bound in deadlines.ACTORS.items():
        if bound.time_limit is not None:
            assert bound.time_limit + blocking_call < bound.lease, name
            assert bound.time_limit > deadlines.LAST_REQUEST_START + deadlines.PROVIDER_REQUEST


def test_the_time_limit_is_a_backstop_above_the_measured_fit() -> None:
    calibration = deadlines.ACTORS["calibrate_plant"]

    assert calibration.time_limit is not None
    assert calibration.time_limit > 4 * deadlines.CALIBRATION_FIT


def test_job_leases_are_the_ladder_leases() -> None:
    assert deadlines.ACTORS["collect_forecast"].lease == collection_jobs.LEASE_DURATION
    assert deadlines.ACTORS["collect_epias"].lease == metering_jobs.LEASE_DURATION
    assert deadlines.ACTORS["calibrate_plant"].lease == training.LEASE_DURATION
    assert deadlines.ACTORS["send_forecast_reminder"].lease == notification_jobs.LEASE_DURATION


def test_the_drain_outlasts_every_actor_and_the_stop_grace_outlasts_the_drain() -> None:
    for name, bound in deadlines.ACTORS.items():
        longest = (
            bound.lease + deadlines.MAIL_EXCHANGE
            if bound.time_limit is None
            else bound.time_limit + deadlines.LEASE_MARGIN
        )
        assert longest <= deadlines.DRAIN, name
    assert deadlines.STOP_GRACE > deadlines.DRAIN
    services = compose_services()
    for name in ("worker", "notifications", "dispatcher"):
        assert duration(services[name]["stop_grace_period"]) >= deadlines.STOP_GRACE, name


def test_the_worker_drains_for_the_drain_bound() -> None:
    assert deadlines.DRAIN.total_seconds() * 1000 == worker.DRAIN_MILLISECONDS


def test_the_dispatcher_polls_well_inside_its_heartbeat_lag() -> None:
    dispatcher = compose_services()["dispatcher"]
    command = dispatcher["command"]
    poll = float(command[command.index("--poll-seconds") + 1])
    lag = re.search(r"> (\d+)\)", dispatcher["healthcheck"]["test"][-1])
    parser = dispatch.Command().create_parser("pyracast", "dispatch")

    assert lag
    assert poll == deadlines.DISPATCH_POLL_SECONDS
    assert parser.get_default("poll_seconds") == deadlines.DISPATCH_POLL_SECONDS
    assert timedelta(seconds=int(lag[1])) == deadlines.HEARTBEAT_LAG
    assert timedelta(seconds=2 * poll) < deadlines.HEARTBEAT_LAG


def test_database_sessions_carry_the_ladder_timeouts() -> None:
    database = settings.DATABASES["default"]
    options = database["OPTIONS"]["options"]

    assert database["CONN_HEALTH_CHECKS"] is True
    assert database["OPTIONS"]["connect_timeout"] == deadlines.DATABASE_CONNECT_SECONDS
    assert f"-c lock_timeout={deadlines.DATABASE_LOCK_SECONDS}s" in options
    assert f"-c statement_timeout={deadlines.DATABASE_STATEMENT_SECONDS}s" in options
    assert (
        f"-c idle_in_transaction_session_timeout={deadlines.DATABASE_IDLE_IN_TRANSACTION_SECONDS}s"
        in options
    )
    assert (
        deadlines.DATABASE_LOCK_SECONDS
        < deadlines.DATABASE_STATEMENT_SECONDS
        < deadlines.DATABASE_IDLE_IN_TRANSACTION_SECONDS
    )


def test_mail_sockets_use_the_ladder_timeout() -> None:
    options = settings.MAILERS["default"]["OPTIONS"]

    assert options["timeout"] == deadlines.MAIL_SOCKET_SECONDS


def test_publication_stops_ten_batches_deep() -> None:
    for pending in (
        collection_jobs.pending_job_ids,
        metering_jobs.pending_epias_ids,
        notification_jobs.pending_notification_ids,
        pending_calibration_ids,
    ):
        assert inspect.signature(pending).parameters["limit"].default == deadlines.PUBLISH_BATCH
    assert deadlines.QUEUE_DEPTH_LIMIT == 10 * deadlines.PUBLISH_BATCH


def test_the_development_launcher_waits_for_the_stop_grace() -> None:
    stop = inspect.signature(launcher.stop_cooperatively).parameters["timeout"].default
    jobs = inspect.signature(launcher.wait_for_jobs).parameters["timeout"].default

    assert stop == deadlines.STOP_GRACE.total_seconds()
    assert jobs == deadlines.DRAIN.total_seconds()
