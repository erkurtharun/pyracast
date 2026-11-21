import json
from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from pyracast.orchestration.delivery_health import DISPATCH_LAG, delivery_health, read_broker


class Command(BaseCommand):
    help = "Report dispatcher and delivery health as JSON; fail for missing or degraded work."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--max-lag-minutes", type=int, default=int(DISPATCH_LAG.total_seconds() // 60)
        )

    def handle(self, *args: Any, **options: Any) -> None:
        minutes = options["max_lag_minutes"]
        if minutes < 1:
            raise CommandError("--max-lag-minutes must be positive")
        broker = read_broker()
        # The host watchdog gives this check a bounded slot; a slow query must fail inside it.
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '10s'")
            report = delivery_health(timezone.now(), timedelta(minutes=minutes), broker=broker)
        self.stdout.write(json.dumps(report.as_json(), sort_keys=True))
        if not report.healthy:
            raise CommandError("Collection or notification delivery needs attention.")
