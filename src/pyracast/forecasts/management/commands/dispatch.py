import time
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from pyracast.notifications.runtime import dispatch_notifications, make_notification_actor
from pyracast.plants.runtime import dispatch_calibrations, make_calibration_actor
from pyracast.runtime import dispatch, make_actor, make_broker


class Command(BaseCommand):
    help = "Schedule and dispatch forecasts, calibrations and email reminders."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=60.0)

    def handle(self, *args: Any, **options: Any) -> None:
        interval = options["poll_seconds"]
        if interval <= 0:
            raise CommandError("--poll-seconds must be positive")
        broker = make_broker()
        actor = make_actor(broker)
        calibration_actor = make_calibration_actor(broker)
        notification_actor = make_notification_actor(broker)
        try:
            while True:
                dispatch(actor)
                dispatch_calibrations(calibration_actor)
                dispatch_notifications(notification_actor)
                if not options["loop"]:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
        finally:
            broker.close()
