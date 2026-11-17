from pathlib import Path
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from pyracast.forecasts.historical_import import import_historical_reference, parse_source_settings
from pyracast.irradiance.pairs import load_json_file


class Command(BaseCommand):
    help = "Archive an authorized historical rooftop PV export with explicit source settings."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument("--tenant-id", required=True, type=int)
        parser.add_argument("--region-id", required=True, type=int)
        parser.add_argument("--binding-id", required=True, type=int)
        parser.add_argument("--power-file", required=True, type=Path)
        parser.add_argument("--manifest", required=True, type=Path)

    def handle(self, *args: Any, **options: Any) -> str:
        try:
            actor = User.objects.get(username=options["username"])
            declaration, _ = load_json_file(options["manifest"])
            record = import_historical_reference(
                actor,
                options["tenant_id"],
                options["region_id"],
                options["binding_id"],
                options["power_file"],
                parse_source_settings(declaration),
            )
        except (ObjectDoesNotExist, PermissionDenied, ValidationError, ValueError) as exc:
            raise CommandError("Historical reference import failed: " + str(exc)) from exc
        return (
            f"Historical reference import {record.pk}: {record.interval_count} native intervals; "
            f"SHA256 {record.sha256}; acquired {record.imported_at.isoformat()}"
        )
