import math
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from pyracast.background.deadlines import DISPATCH_POLL_SECONDS
from pyracast.orchestration import dispatcher
from pyracast.shutdown import shutdown_control


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
        with shutdown_control(options.get("stop_file")) as shutdown:
            if shutdown.requested:
                return
            dispatcher.run(
                shutdown,
                loop=options["loop"],
                interval=interval,
                heartbeat=options.get("heartbeat_file"),
            )
