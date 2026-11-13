from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from pyracast.plants.models import Plant
from pyracast.plants.operations import activate_calibration, reset_calibration


class Command(BaseCommand):
    help = "Activate a completed calibration or restore the baseline."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument("--plant-id", required=True, type=int)
        choice = parser.add_mutually_exclusive_group(required=True)
        choice.add_argument("--calibration-id", type=int)
        choice.add_argument("--baseline", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            actor = User.objects.get(username=options["username"])
            plant = Plant.objects.select_related("region").get(pk=options["plant_id"])
            scope = (actor, plant.region.tenant_id, plant.region_id, plant.pk)
            if options["baseline"]:
                activation = reset_calibration(*scope)
            else:
                activation = activate_calibration(*scope, options["calibration_id"])
        except (ObjectDoesNotExist, PermissionDenied, ValidationError) as exc:
            raise CommandError("Activation failed: invalid input, access, or record.") from exc
        self.stdout.write(
            f"Activation {activation.pk}; calibration {activation.calibration_id or 'baseline'}."
        )
