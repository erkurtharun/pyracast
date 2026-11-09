from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from pyracast.plants.operations import create_plant, revise_plant
from pyracast.regions.models import Region


class Command(BaseCommand):
    help = "Create or revise a tenant plant configuration."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument("--region-id", required=True, type=int)
        parser.add_argument("--name", required=True)
        parser.add_argument("--dc-capacity-mw", required=True, type=float)
        parser.add_argument("--inverter-capacity-mw", type=float)
        parser.add_argument("--export-limit-mw", type=float)
        parser.add_argument(
            "--measurement-basis", choices=("net_export", "ac_generation"), default="net_export"
        )
        parser.add_argument("--plant-id", type=int)

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            actor = User.objects.get(username=options["username"])
            region = Region.objects.get(pk=options["region_id"])
            values = {
                key: options[key]
                for key in (
                    "name",
                    "dc_capacity_mw",
                    "inverter_capacity_mw",
                    "export_limit_mw",
                    "measurement_basis",
                )
            }
            if options["plant_id"] is None:
                plant = create_plant(actor, region.tenant_id, region.pk, **values)
            else:
                plant = revise_plant(
                    actor, region.tenant_id, region.pk, options["plant_id"], **values
                )
        except (ObjectDoesNotExist, PermissionDenied, ValidationError) as exc:
            raise CommandError(
                "Plant configuration failed: invalid input, access, or record."
            ) from exc
        self.stdout.write(f"Plant {plant.pk}, configuration {plant.current_configuration_id}.")
