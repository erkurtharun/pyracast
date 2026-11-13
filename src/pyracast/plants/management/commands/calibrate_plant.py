from datetime import date
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from pyracast.plants.models import Plant
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
        try:
            actor = User.objects.get(username=options["username"])
            plant = Plant.objects.select_related("region").get(pk=options["plant_id"])
            run = request_calibration(
                actor,
                plant.region.tenant_id,
                plant.region_id,
                plant.pk,
                configuration_id=options["configuration_id"],
                start_date=date.fromisoformat(options["start_date"]),
                end_date=date.fromisoformat(options["end_date"]),
            )
            if options["run"]:
                run_calibration(run.pk)
                run.refresh_from_db()
        except (ValueError, ObjectDoesNotExist, PermissionDenied, ValidationError) as exc:
            raise CommandError("Calibration failed: invalid input, access, or record.") from exc
        self.stdout.write(
            f"Calibration {run.pk}: {run.status}; assessment: {run.assessment or 'pending'}. "
            "No activation was performed."
        )
