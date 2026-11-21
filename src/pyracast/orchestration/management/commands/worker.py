from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from dramatiq import Worker

from pyracast.background.broker import make_broker
from pyracast.background.deadlines import DRAIN
from pyracast.orchestration.kinds import QUEUES, WORKER_QUEUES, register_actors
from pyracast.shutdown import shutdown_control

DRAIN_MILLISECONDS = round(DRAIN.total_seconds() * 1000)


class Command(BaseCommand):
    help = "Run threaded RabbitMQ forecast, production, calibration and notification workers."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--threads", type=int, default=4)
        parser.add_argument("--queues", nargs="+", choices=QUEUES, default=WORKER_QUEUES)
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
                register_actors(broker)
                # The Dramatiq CLI emits this; without it the TimeLimit middleware never runs.
                broker.emit_after("process_boot")
                worker = Worker(broker, worker_threads=threads, queues=set(options["queues"]))
                try:
                    worker.start()
                    shutdown.wait()
                except KeyboardInterrupt:
                    pass
                finally:
                    worker.stop(timeout=DRAIN_MILLISECONDS)
            finally:
                broker.close()
