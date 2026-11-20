import inspect
import math
import re
import runpy
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import yaml
from django.conf import LazySettings, settings
from dramatiq.brokers.stub import StubBroker

from pyracast import outbound
from pyracast.background import broker as broker_module
from pyracast.background import deadlines
from pyracast.forecasts import jobs as collection_jobs
from pyracast.metering import jobs as metering_jobs
from pyracast.notifications import jobs as notification_jobs
from pyracast.notifications import outbox
from pyracast.orchestration.kinds import register_actors
from pyracast.orchestration.management.commands import dispatch, worker
from pyracast.plants import training
from pyracast.plants.training import pending_calibration_ids
from scripts import dev as launcher

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SETTINGS = DEPLOY.parent / "src" / "pyracast" / "settings.py"
MAIL_ACTORS = ("send_forecast_reminder", "send_outbound_mail")


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
    register_actors(broker)
    return dict(broker.actors)


def test_every_registered_actor_runs_under_its_bound() -> None:
    actors = registered_actors()

    assert set(actors) == set(deadlines.ACTORS)
    for name, actor in actors.items():
        bound = deadlines.ACTORS[name]
        expected = math.inf if bound.time_limit is None else bound.time_limit.total_seconds() * 1000
        assert actor.options["time_limit"] == expected, name


def test_mail_actors_are_never_interrupted_after_their_transmit_marker() -> None:
    actors = registered_actors()

    for name in MAIL_ACTORS:
        assert deadlines.ACTORS[name].time_limit is None, name
        assert actors[name].options["time_limit"] == math.inf, name


def test_an_interrupted_actor_stops_before_its_lease_can_be_claimed_again() -> None:
    blocking_call = timedelta(
        seconds=max(
            deadlines.DATABASE_STATEMENT_SECONDS,
            deadlines.HTTP_CONNECT_SECONDS + deadlines.HTTP_READ_SECONDS,
            deadlines.MAIL_SOCKET_SECONDS,
        )
    )
    commit = timedelta(seconds=deadlines.DATABASE_STATEMENT_SECONDS)

    assert timedelta(0) < deadlines.LAST_REQUEST_START
    for name, bound in deadlines.ACTORS.items():
        if bound.time_limit is not None:
            assert bound.time_limit + blocking_call < bound.lease, name
            assert (
                deadlines.LAST_REQUEST_START + deadlines.PROVIDER_REQUEST + commit
                <= bound.time_limit
            ), name


def test_a_mail_exchange_outlasts_each_of_its_socket_operations() -> None:
    for name in MAIL_ACTORS:
        assert deadlines.ACTORS[name].time_limit is None, name
    assert timedelta(0) < timedelta(seconds=deadlines.MAIL_SOCKET_SECONDS) < deadlines.MAIL_EXCHANGE


def test_a_provider_request_ends_within_its_body_deadline_and_one_read() -> None:
    assert (
        0
        < deadlines.HTTP_CONNECT_SECONDS
        < deadlines.HTTP_READ_SECONDS
        <= deadlines.HTTP_BODY_SECONDS
    )
    assert (
        timedelta(seconds=deadlines.HTTP_BODY_SECONDS + deadlines.HTTP_READ_SECONDS)
        <= deadlines.PROVIDER_REQUEST
    )
    assert (
        httpx.Timeout(deadlines.HTTP_READ_SECONDS, connect=deadlines.HTTP_CONNECT_SECONDS)
        == outbound.TIMEOUT
    )


class Chunks(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield from (b"a", b"b", b"c")


@pytest.mark.parametrize(
    ("elapsed", "complete"),
    [
        ((1, deadlines.HTTP_BODY_SECONDS, deadlines.HTTP_BODY_SECONDS), True),
        ((1, 2, deadlines.HTTP_BODY_SECONDS + 1), False),
    ],
)
def test_an_answer_body_is_read_until_the_ladder_body_deadline(
    monkeypatch: pytest.MonkeyPatch, elapsed: tuple[int, ...], complete: bool
) -> None:
    clock = iter(elapsed)
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    response = httpx.Response(200, stream=Chunks())

    if complete:
        assert outbound.read_bounded(response, 0) == b"abc"
    else:
        with pytest.raises(outbound.UnboundedResponseError) as error:
            outbound.read_bounded(response, 0)
        assert error.value.too_large is False


def test_the_calibration_time_limit_is_a_backstop_above_the_measured_fit() -> None:
    calibration = deadlines.ACTORS["calibrate_plant"]

    assert calibration.time_limit is not None
    assert calibration.time_limit > 4 * deadlines.CALIBRATION_FIT


def test_job_leases_are_the_ladder_leases() -> None:
    assert deadlines.ACTORS["collect_forecast"].lease == collection_jobs.LEASE_DURATION
    assert deadlines.ACTORS["collect_epias"].lease == metering_jobs.LEASE_DURATION
    assert deadlines.ACTORS["calibrate_plant"].lease == training.LEASE_DURATION
    assert deadlines.ACTORS["send_forecast_reminder"].lease == notification_jobs.LEASE_DURATION
    assert deadlines.ACTORS["send_outbound_mail"].lease == outbox.LEASE_DURATION


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


def test_the_smoke_drains_the_dispatcher_before_its_consumers() -> None:
    stops = [
        tuple(match[1].split())
        for match in re.finditer(
            r'^"\$\{compose\[@\]\}" stop -t \d+ ([a-z ]+)$',
            (DEPLOY / "smoke.sh").read_text(encoding="utf-8"),
            re.M,
        )
    ]
    background = [stop for stop in stops if {"dispatcher", "worker", "notifications"} & set(stop)]

    assert background
    for index, stop in enumerate(background):
        if "dispatcher" in stop:
            assert stop == ("dispatcher",)
            assert set(background[index + 1]) == {"worker", "notifications"}


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


def test_a_dispatcher_without_a_broker_is_restarted_within_the_outage_bound() -> None:
    services = compose_services()
    health = services["dispatcher"]["healthcheck"]
    broker = services["rabbitmq"]["healthcheck"]
    broker_starting = duration(broker["start_period"]) + broker["retries"] * duration(
        broker["interval"]
    )
    # The EPIAS settlement directory loads within one phase with up to two provider requests.
    longest_phase_step = 2 * deadlines.PROVIDER_REQUEST

    assert duration(health["interval"]) == deadlines.DISPATCHER_HEALTH_INTERVAL
    assert health["retries"] == deadlines.DISPATCHER_HEALTH_RETRIES
    assert (
        deadlines.BROKER_OUTAGE_RESTART
        == deadlines.HEARTBEAT_LAG
        + deadlines.DISPATCHER_HEALTH_RETRIES * deadlines.DISPATCHER_HEALTH_INTERVAL
    )
    assert broker_starting < deadlines.HEARTBEAT_LAG < deadlines.BROKER_OUTAGE_RESTART
    assert longest_phase_step > timedelta(seconds=2 * deadlines.BROKER_HEARTBEAT_SECONDS)
    assert (
        timedelta(seconds=deadlines.DISPATCH_POLL_SECONDS) + longest_phase_step
        < deadlines.HEARTBEAT_LAG
    )


def test_database_sessions_carry_the_ladder_timeouts() -> None:
    database = settings.DATABASES["default"]
    session = cast("dict[str, object]", database["OPTIONS"])
    options = str(session["options"])

    assert database["CONN_HEALTH_CHECKS"] is True
    assert session["connect_timeout"] == deadlines.DATABASE_CONNECT_SECONDS
    assert f"-c lock_timeout={deadlines.DATABASE_LOCK_SECONDS}s" in options
    assert f"-c statement_timeout={deadlines.DATABASE_STATEMENT_SECONDS}s" in options
    assert (
        f"-c idle_in_transaction_session_timeout={deadlines.DATABASE_IDLE_IN_TRANSACTION_SECONDS}s"
        in options
    )
    assert (
        deadlines.DATABASE_CONNECT_SECONDS
        <= deadlines.DATABASE_LOCK_SECONDS
        < deadlines.DATABASE_STATEMENT_SECONDS
        < deadlines.DATABASE_IDLE_IN_TRANSACTION_SECONDS
    )


def test_the_broker_connection_carries_the_ladder_bounds(
    monkeypatch: pytest.MonkeyPatch, settings: LazySettings
) -> None:
    constructor = MagicMock()
    monkeypatch.setattr(broker_module, "RabbitmqBroker", constructor)
    settings.PYRACAST_RABBITMQ_URL = "amqp://example.invalid/"

    broker_module.make_broker()

    parameters = parse_qs(urlsplit(constructor.call_args.kwargs["url"]).query)
    assert parameters["socket_timeout"] == [str(deadlines.BROKER_SOCKET_SECONDS)]
    assert parameters["stack_timeout"] == [str(deadlines.BROKER_CALL_SECONDS)]
    assert parameters["blocked_connection_timeout"] == [str(deadlines.BROKER_CALL_SECONDS)]
    assert parameters["heartbeat"] == [str(deadlines.BROKER_HEARTBEAT_SECONDS)]
    assert (
        deadlines.BROKER_SOCKET_SECONDS
        < deadlines.BROKER_CALL_SECONDS
        < deadlines.BROKER_HEARTBEAT_SECONDS
    )


@pytest.mark.parametrize("backend", ["smtp", "gmail"])
def test_mail_sockets_use_the_ladder_timeout(monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    monkeypatch.setenv("PYRACAST_EMAIL_BACKEND", backend)
    mailers = runpy.run_path(str(SETTINGS))["MAILERS"]

    assert mailers["default"]["OPTIONS"]["timeout"] == deadlines.MAIL_SOCKET_SECONDS


def test_publication_stops_ten_batches_deep() -> None:
    for pending in (
        collection_jobs.pending_job_ids,
        metering_jobs.pending_epias_ids,
        notification_jobs.pending_notification_ids,
        pending_calibration_ids,
        outbox.pending_mail_ids,
        outbox.publish_due_mail,
    ):
        assert inspect.signature(pending).parameters["limit"].default == deadlines.PUBLISH_BATCH
    assert deadlines.QUEUE_DEPTH_LIMIT == 10 * deadlines.PUBLISH_BATCH


def test_the_development_launcher_waits_for_the_stop_grace() -> None:
    stop = inspect.signature(launcher.stop_cooperatively).parameters["timeout"].default
    jobs = inspect.signature(launcher.wait_for_jobs).parameters["timeout"].default

    assert stop == deadlines.STOP_GRACE.total_seconds()
    assert jobs == deadlines.DRAIN.total_seconds()
