import logging
import math
from functools import partial
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone

from pyracast.forecasts.monitoring import record_dispatch_phase
from pyracast.metering.runtime import dispatch_epias, make_epias_actor
from pyracast.notifications.runtime import dispatch_notifications, make_notification_actor
from pyracast.plants.runtime import dispatch_calibrations, make_calibration_actor
from pyracast.runtime import dispatch, make_actor, make_broker
from pyracast.shutdown import shutdown_control
from pyracast.trash import purge_expired

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Schedule and dispatch forecasts, reported production, calibrations and email reminders."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=60.0)
        parser.add_argument("--stop-file", type=Path)

    def handle(self, *args: Any, **options: Any) -> None:
        interval = options["poll_seconds"]
        if not math.isfinite(interval) or interval <= 0:
            raise CommandError("--poll-seconds must be positive and finite")
        with shutdown_control(options.get("stop_file")) as shutdown:
            if shutdown.requested:
                return
            broker = make_broker()
            try:
                actor = make_actor(broker)
                calibration_actor = make_calibration_actor(broker)
                notification_actor = make_notification_actor(broker)
                epias_actor = make_epias_actor(broker)
                while not shutdown.requested:
                    failures = []
                    for name, operation in (
                        ("notifications", partial(dispatch_notifications, notification_actor)),
                        ("collection", partial(dispatch, actor)),
                        ("calibration", partial(dispatch_calibrations, calibration_actor)),
                        ("retention", purge_expired),
                        ("metering", partial(dispatch_epias, epias_actor)),
                    ):
                        try:
                            close_old_connections()
                            operation()
                        except Exception as error:
                            failures.append(name)
                            logger.error(
                                "dispatch_phase_failed phase=%s error_type=%s",
                                name,
                                type(error).__name__,
                            )
                            error_code = type(error).__name__
                        else:
                            error_code = ""
                        try:
                            record_dispatch_phase(name, timezone.now(), error_code)
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
