from dataclasses import fields
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from pyracast.plants.operations import create_plant, revise_plant_outcome
from pyracast.plants.parameters import PlantParameters
from pyracast.regions.models import Region


class Command(BaseCommand):
    help = (
        "Create a tenant plant, or revise one with --plant-id. A revision replaces every value: "
        "an omitted option takes its default. Without --expected-revision it overwrites the "
        "plant whatever changed meanwhile; with it, a plant at another parameters revision "
        "refuses the change."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument("--region-id", required=True, type=int)
        parser.add_argument("--name", required=True)
        parser.add_argument("--dc-capacity-mw", required=True, type=float)
        parser.add_argument("--inverter-capacity-mw", type=float)
        parser.add_argument("--export-limit-mw", type=float)
        parser.add_argument(
            "--mounting-type", choices=("unknown", "fixed", "tracking"), default="unknown"
        )
        parser.add_argument(
            "--surface-tilt-degrees", type=float, help="Fixed surface tilt in degrees, 0 to 90."
        )
        parser.add_argument(
            "--surface-azimuth-degrees",
            type=float,
            help="Fixed surface azimuth in degrees clockwise from north, 0 to below 360.",
        )
        parser.add_argument(
            "--measurement-basis", choices=("net_export", "ac_generation"), default="net_export"
        )
        parser.add_argument("--plant-id", type=int)
        parser.add_argument(
            "--expected-revision",
            type=int,
            help="With --plant-id: the parameters revision the change was made from, 1 or more.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        expected = options["expected_revision"]
        if expected is not None and options["plant_id"] is None:
            raise CommandError(
                "Plant configuration failed: --expected-revision requires --plant-id."
            )
        if expected is not None and expected < 1:
            raise CommandError(
                "Plant configuration failed: --expected-revision must be at least 1."
            )
        try:
            actor = User.objects.get(username=options["username"])
            region = Region.objects.get(pk=options["region_id"])
            parameters = PlantParameters(
                **{field.name: options[field.name] for field in fields(PlantParameters)}
            )
            if options["plant_id"] is None:
                plant = create_plant(actor, region.tenant_id, region.pk, parameters)
                revisions = str(plant.parameters_revision)
            else:
                outcome = revise_plant_outcome(
                    actor,
                    region.tenant_id,
                    region.pk,
                    options["plant_id"],
                    parameters,
                    expected_revision=expected,
                )
                plant = outcome.plant
                revisions = (
                    f"{outcome.before} to {plant.parameters_revision}"
                    if outcome.changed
                    else f"{plant.parameters_revision} unchanged"
                )
        except ValidationError as exc:
            raise CommandError(f"Plant configuration failed: {'; '.join(exc.messages)}") from exc
        except (ObjectDoesNotExist, PermissionDenied) as exc:
            raise CommandError(
                "Plant configuration failed: unknown or inaccessible record."
            ) from exc
        self.stdout.write(
            f"Plant {plant.pk}, configuration {plant.current_configuration_id}, "
            f"parameters revision {revisions}."
        )
