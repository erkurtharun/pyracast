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

from pyracast.background.failures import log_failure
from pyracast.forecasts.monitoring import PhaseOutcome, record_dispatch_phase
from pyracast.metering.runtime import dispatch_epias, make_epias_actor
from pyracast.notifications.runtime import dispatch_notifications, make_notification_actor
from pyracast.plants.runtime import dispatch_calibrations, make_calibration_actor
from pyracast.runtime import dispatch, make_actor, make_broker
from pyracast.shutdown import shutdown_control
from pyracast.trash import purge_expired

logger = logging.getLogger(__name__)


def _published(operation: Callable[[], int]) -> Callable[[], PhaseOutcome]:
    return lambda: PhaseOutcome(published=operation())


def _retention() -> PhaseOutcome:
    purge_expired()
    return PhaseOutcome()


class Command(BaseCommand):
    help = "Schedule and dispatch forecasts, reported production, calibrations and email reminders."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=60.0)
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
                epias_actor = make_epias_actor(broker)
                phases: tuple[tuple[str, Callable[[], PhaseOutcome]], ...] = (
                    (
                        "notifications",
                        _published(partial(dispatch_notifications, notification_actor)),
                    ),
                    ("collection", partial(dispatch, actor)),
                    ("calibration", _published(partial(dispatch_calibrations, calibration_actor))),
                    ("retention", _retention),
                    ("metering", _published(partial(dispatch_epias, epias_actor))),
                )
                while not shutdown.requested:
                    if heartbeat is not None:
                        heartbeat.touch()
                    failures = []
                    for name, operation in phases:
                        started = time.monotonic()
                        try:
                            close_old_connections()
                            outcome = operation()
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
                            logger.error(
                                "dispatch_monitoring_failed phase=%s error_type=%s",
                                name,
                                type(error).__name__,
                            )
                    if failures and not options["loop"]:
                        raise CommandError("Failed dispatch phase(s): " + ", ".join(failures))
                    if not options["loop"] or shutdown.wait(interval):
                        break
            except KeyboardInterrupt:
                pass
            finally:
                broker.close()
