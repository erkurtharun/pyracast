import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from pyracast.plants.models import Plant
from pyracast.plants.operations import ObservationInput, import_observations


class Command(BaseCommand):
    help = "Import completed production observations from a CSV file inside the working directory."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument("--plant-id", required=True, type=int)
        parser.add_argument("--configuration-id", required=True, type=int)
        parser.add_argument("--unit", choices=("MWh", "kWh"), required=True)
        parser.add_argument("--file", required=True, type=Path)

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            path = options["file"].resolve(strict=True)
            path.relative_to(Path.cwd().resolve())
            if (
                path.suffix.lower() != ".csv"
                or not path.is_file()
                or path.stat().st_size > 2 * 1024 * 1024
            ):
                raise ValueError("Invalid file")
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream, strict=True)
                headers = reader.fieldnames
                required = {"starts_at", "ends_at", "energy"}
                if (
                    headers is None
                    or len(headers) != len(set(headers))
                    or not required <= set(headers)
                    or set(headers) - (required | {"quality", "source"})
                ):
                    raise ValueError("Invalid headers")
                rows: list[ObservationInput] = []
                for row in reader:
                    if (
                        len(rows) >= 2000
                        or None in row
                        or any(value is None for value in row.values())
                    ):
                        raise ValueError("Invalid row")
                    rows.append(
                        ObservationInput(
                            starts_at=datetime.fromisoformat(row["starts_at"]),
                            ends_at=datetime.fromisoformat(row["ends_at"]),
                            energy=float(row["energy"]),
                            quality=row.get("quality") or "valid",
                            source=row.get("source") or "operator",
                        )
                    )
            actor = User.objects.get(username=options["username"])
            plant = Plant.objects.select_related("region").get(pk=options["plant_id"])
            result = import_observations(
                actor,
                plant.region.tenant_id,
                plant.region_id,
                plant.pk,
                configuration_id=options["configuration_id"],
                unit=options["unit"],
                observations=rows,
            )
        except (
            OSError,
            UnicodeError,
            csv.Error,
            ValueError,
            TypeError,
            ObjectDoesNotExist,
            PermissionDenied,
            ValidationError,
        ) as exc:
            raise CommandError(
                "Observation import failed: invalid file, input, access, or record."
            ) from exc
        self.stdout.write(f"Imported {result['created']}; existing {result['existing']}.")
