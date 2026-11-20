from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from pyracast.plants.calibration_runs import OPEN_STATUSES
from pyracast.plants.management.operator import operator_scope, refusals
from pyracast.plants.operations import request_calibration
from pyracast.plants.training import run_calibration


class Command(BaseCommand):
    help = "Request a calibration and optionally run it offline."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument("--plant-id", required=True, type=int)
        parser.add_argument("--configuration-id", required=True, type=int)
        parser.add_argument("--start-date", required=True)
        parser.add_argument("--end-date", required=True)
        parser.add_argument("--run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        scope = operator_scope(options["username"], options["plant_id"])
        try:
            start, end = (
                date.fromisoformat(options["start_date"]),
                date.fromisoformat(options["end_date"]),
            )
        except ValueError as error:
            raise CommandError("Use ISO dates (YYYY-MM-DD).") from error
        with refusals("Calibration", options["username"], options["plant_id"]):
            run = request_calibration(
                *scope, configuration_id=options["configuration_id"], start_date=start, end_date=end
            )
        if options["run"]:
            run_calibration(run.pk)
            run.refresh_from_db()
            if run.status in OPEN_STATUSES:
                raise CommandError(
                    f"Calibration {run.pk} is held by another worker or waits for its cutoff."
                )
        self.stdout.write(
            f"Calibration {run.pk}: {run.status}; assessment: {run.assessment or 'pending'}. "
            "No activation was performed."
        )
