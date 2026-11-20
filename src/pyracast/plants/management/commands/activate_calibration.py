from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from pyracast.plants.activation import activate_calibration, reset_calibration
from pyracast.plants.management.operator import operator_scope, refusals
from pyracast.plants.models import CalibrationRun


class Command(BaseCommand):
    help = "Activate a completed calibration or restore the baseline."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument("--plant-id", required=True, type=int)
        choice = parser.add_mutually_exclusive_group(required=True)
        choice.add_argument("--calibration-id", type=int)
        choice.add_argument("--baseline", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        scope = operator_scope(options["username"], options["plant_id"])
        with refusals("Activation", options["username"], options["plant_id"]):
            try:
                activation = (
                    reset_calibration(*scope)
                    if options["baseline"]
                    else activate_calibration(*scope, options["calibration_id"])
                )
            except CalibrationRun.DoesNotExist as error:
                raise CommandError(
                    f"Calibration {options['calibration_id']} not found for the current "
                    "configuration."
                ) from error
        self.stdout.write(
            f"Activation {activation.pk}; calibration {activation.calibration_id or 'baseline'}."
        )
