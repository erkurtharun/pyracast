import logging
import time
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path

from django.core.management.base import CommandError
from django.db import close_old_connections
from django.utils import timezone
from dramatiq import Actor

from pyracast.accounts.retention import sweep_accounts
from pyracast.background.broker import make_broker
from pyracast.background.failures import (
    ItemsFailedError,
    PhaseOutcome,
    broker_answers,
    log_failure,
)
from pyracast.forecasts.monitoring import record_dispatch_phase
from pyracast.forecasts.runtime import dispatch_collections
from pyracast.metering.runtime import dispatch_epias
from pyracast.notifications.outbox import purge_finished_mail
from pyracast.notifications.runtime import dispatch_notifications, publish_outbound_mail
from pyracast.orchestration.kinds import (
    CALIBRATION,
    COLLECTION,
    METERING,
    OUTBOUND_MAIL,
    REMINDER,
    register_actors,
)
from pyracast.plants.runtime import dispatch_calibrations
from pyracast.shutdown import ShutdownRequest
from pyracast.trash import purge_expired

logger = logging.getLogger(__name__)
# Mail waits for a code in an open browser, so it is published between the regular cycles too.
MAIL_TICK_SECONDS = 2.0
Phase = tuple[str, Callable[[], PhaseOutcome]]


def _published(operation: Callable[[], int]) -> Callable[[], PhaseOutcome]:
    return lambda: PhaseOutcome(published=operation())


def _retention() -> PhaseOutcome:
    # Item failures of one step do not skip the others; a step that fails as a whole outranks
    # them because its items are already logged one by one.
    now = timezone.now()
    failed = 0
    for step in (purge_expired, sweep_accounts, purge_finished_mail):
        try:
            step(now)
        except ItemsFailedError as error:
            failed += error.count
    if failed:
        raise ItemsFailedError(failed)
    return PhaseOutcome()


def phases(actors: Mapping[str, Actor[[int], None]]) -> tuple[Phase, ...]:
    return (
        (
            "notifications",
            _published(
                partial(dispatch_notifications, actors[REMINDER.actor], actors[OUTBOUND_MAIL.actor])
            ),
        ),
        ("collection", partial(dispatch_collections, actors[COLLECTION.actor])),
        ("calibration", _published(partial(dispatch_calibrations, actors[CALIBRATION.actor]))),
        ("retention", _retention),
        ("metering", _published(partial(dispatch_epias, actors[METERING.actor]))),
    )


def _run_phase(name: str, operation: Callable[[], PhaseOutcome]) -> list[str]:
    started = time.monotonic()
    try:
        close_old_connections()
        outcome = operation()
    except ItemsFailedError as error:
        outcome = PhaseOutcome(published=error.published, error=f"items_failed:{error.count}")
    except Exception as error:
        log_failure(logger, "dispatch_phase_failed", error, phase=name)
        outcome = PhaseOutcome(error=type(error).__name__)
    duration_ms = round((time.monotonic() - started) * 1000)
    logger.info(
        "dispatch_phase_finished phase=%s duration_ms=%s published=%s error=%s",
        name,
        duration_ms,
        outcome.published,
        outcome.error,
    )
    failures = [name] if outcome.error else []
    try:
        close_old_connections()
        record_dispatch_phase(
            name,
            timezone.now(),
            outcome.error,
            duration_ms=duration_ms,
            published=outcome.published,
        )
    except Exception as error:
        failures.append("monitoring")
        log_failure(logger, "dispatch_monitoring_failed", error, phase=name)
    return failures


def _wait_publishing_mail(
    shutdown: ShutdownRequest, interval: float, publish: Callable[[], object]
) -> bool:
    deadline = time.monotonic() + interval
    while (remaining := deadline - time.monotonic()) > 0:
        if shutdown.wait(min(MAIL_TICK_SECONDS, remaining)):
            return True
        try:
            close_old_connections()
            publish()
        except Exception as error:
            log_failure(logger, "mail_publication_failed", error)
    return False


def run(shutdown: ShutdownRequest, *, loop: bool, interval: float, heartbeat: Path | None) -> None:
    broker = make_broker()
    try:
        actors = register_actors(broker)
        cycle = phases(actors)
        while not shutdown.requested:
            # Without a broker answer the heartbeat ages, so the container turns unhealthy
            # within BROKER_OUTAGE_RESTART and the watchdog restarts it.
            if heartbeat is not None and broker_answers(actors[COLLECTION.actor], logger):
                heartbeat.touch()
            failures = [
                failure for name, operation in cycle for failure in _run_phase(name, operation)
            ]
            if failures and not loop:
                raise CommandError("Failed dispatch phase(s): " + ", ".join(failures))
            if not loop or _wait_publishing_mail(
                shutdown, interval, partial(publish_outbound_mail, actors[OUTBOUND_MAIL.actor])
            ):
                break
    except KeyboardInterrupt:
        pass
    finally:
        broker.close()
