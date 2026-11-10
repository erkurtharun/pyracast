import threading
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from dramatiq import Worker

from pyracast.metering.runtime import make_epias_actor
from pyracast.notifications.runtime import make_notification_actor
from pyracast.plants.runtime import make_calibration_actor
from pyracast.runtime import make_actor, make_broker


class Command(BaseCommand):
    help = "Run threaded RabbitMQ forecast, production, calibration and notification workers."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--threads", type=int, default=4)

    def handle(self, *args: Any, **options: Any) -> None:
        threads = options["threads"]
        if threads < 1:
            raise CommandError("--threads must be positive")
        broker = make_broker()
        make_actor(broker)
        make_calibration_actor(broker)
        make_notification_actor(broker)
        make_epias_actor(broker)
        worker = Worker(broker, worker_threads=threads)
        try:
            worker.start()
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            worker.stop()
            broker.close()
