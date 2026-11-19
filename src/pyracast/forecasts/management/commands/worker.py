from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from dramatiq import Worker

from pyracast.metering.runtime import make_epias_actor
from pyracast.notifications.runtime import make_notification_actor, make_outbound_mail_actor
from pyracast.plants.runtime import make_calibration_actor
from pyracast.runtime import make_actor, make_broker
from pyracast.shutdown import shutdown_control


class Command(BaseCommand):
    help = "Run threaded RabbitMQ forecast, production, calibration and notification workers."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--threads", type=int, default=4)
        parser.add_argument(
            "--queues",
            nargs="+",
            choices=("default", "calibration", "epias", "notifications"),
            default=("default", "calibration", "epias"),
        )
        parser.add_argument("--stop-file", type=Path)

    def handle(self, *args: Any, **options: Any) -> None:
        threads = options["threads"]
        if threads < 1:
            raise CommandError("--threads must be positive")
        with shutdown_control(options.get("stop_file")) as shutdown:
            if shutdown.requested:
                return
            broker = make_broker()
            try:
                make_actor(broker)
                make_calibration_actor(broker)
                make_notification_actor(broker)
                make_outbound_mail_actor(broker)
                make_epias_actor(broker)
                worker = Worker(broker, worker_threads=threads, queues=set(options["queues"]))
                try:
                    worker.start()
                    shutdown.wait()
                except KeyboardInterrupt:
                    pass
                finally:
                    worker.stop()
            finally:
                broker.close()
