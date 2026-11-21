import logging
import math
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone

from pyracast.accounts.retention import sweep_accounts
from pyracast.background.broker import make_broker
from pyracast.background.deadlines import DISPATCH_POLL_SECONDS
from pyracast.background.failures import (
    ItemsFailedError,
    PhaseOutcome,
    broker_answers,
    log_failure,
)
from pyracast.forecasts.monitoring import record_dispatch_phase
from pyracast.forecasts.runtime import dispatch_collections, make_actor
from pyracast.metering.runtime import dispatch_epias, make_epias_actor
from pyracast.notifications.outbox import purge_finished_mail
from pyracast.notifications.runtime import (
    dispatch_notifications,
    make_notification_actor,
    make_outbound_mail_actor,
    publish_outbound_mail,
)
from pyracast.plants.runtime import dispatch_calibrations, make_calibration_actor
from pyracast.shutdown import ShutdownRequest, shutdown_control
from pyracast.trash import purge_expired

logger = logging.getLogger(__name__)
# Mail waits for a code in an open browser, so it is published between the regular cycles too.
MAIL_TICK_SECONDS = 2.0


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


class Command(BaseCommand):
    help = "Schedule and dispatch forecasts, reported production, calibrations and email reminders."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=DISPATCH_POLL_SECONDS)
        parser.add_argument("--stop-file", type=Path)
        parser.add_argument("--heartbeat-file", type=Path)

    def handle(self, *args: Any, **options: Any) -> None:
        interval = options["poll_seconds"]
        if not math.isfinite(interval) or interval <= 0:
            raise CommandError("--poll-seconds must be positive and finite")
        heartbeat: Path | None = options.get("heartbeat_file")
        with shutdown_control(options.get("stop_file")) as shutdown:
            if shutdown.requested:
                return
            broker = make_broker()
            try:
                actor = make_actor(broker)
                calibration_actor = make_calibration_actor(broker)
                notification_actor = make_notification_actor(broker)
                mail_actor = make_outbound_mail_actor(broker)
                epias_actor = make_epias_actor(broker)
                phases: tuple[tuple[str, Callable[[], PhaseOutcome]], ...] = (
                    (
                        "notifications",
                        _published(partial(dispatch_notifications, notification_actor, mail_actor)),
                    ),
                    ("collection", partial(dispatch_collections, actor)),
                    ("calibration", _published(partial(dispatch_calibrations, calibration_actor))),
                    ("retention", _retention),
                    ("metering", _published(partial(dispatch_epias, epias_actor))),
                )
                while not shutdown.requested:
                    # Without a broker answer the heartbeat ages, so the container turns unhealthy
                    # within BROKER_OUTAGE_RESTART and the watchdog restarts it.
                    if heartbeat is not None and broker_answers(actor, logger):
                        heartbeat.touch()
                    failures = []
                    for name, operation in phases:
                        started = time.monotonic()
                        try:
                            close_old_connections()
                            outcome = operation()
                        except ItemsFailedError as error:
                            outcome = PhaseOutcome(
                                published=error.published, error=f"items_failed:{error.count}"
                            )
                        except Exception as error:
                            log_failure(logger, "dispatch_phase_failed", error, phase=name)
                            outcome = PhaseOutcome(error=type(error).__name__)
                        duration_ms = round((time.monotonic() - started) * 1000)
                        if outcome.error:
                            failures.append(name)
                        logger.info(
                            "dispatch_phase_finished phase=%s duration_ms=%s published=%s error=%s",
                            name,
                            duration_ms,
                            outcome.published,
                            outcome.error,
                        )
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
                    if failures and not options["loop"]:
                        raise CommandError("Failed dispatch phase(s): " + ", ".join(failures))
                    if not options["loop"] or _wait_publishing_mail(
                        shutdown, interval, partial(publish_outbound_mail, mail_actor)
                    ):
                        break
            except KeyboardInterrupt:
                pass
            finally:
                broker.close()
