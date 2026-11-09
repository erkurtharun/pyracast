import time
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from pyracast.runtime import dispatch, make_actor, make_broker


class Command(BaseCommand):
    help = "Schedule due forecasts and send pending jobs to RabbitMQ."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=60.0)

    def handle(self, *args: Any, **options: Any) -> None:
        interval = options["poll_seconds"]
        if interval <= 0:
            raise CommandError("--poll-seconds must be positive")
        broker = make_broker()
        actor = make_actor(broker)
        try:
            while True:
                dispatch(actor)
                if not options["loop"]:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
        finally:
            broker.close()
