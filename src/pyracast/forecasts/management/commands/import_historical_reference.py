from pathlib import Path
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import OperationalError

from pyracast.errors import retryable_database_code
from pyracast.forecasts.historical_upload import import_historical_upload, parse_historical_upload
from pyracast.irradiance.solcast_export import read_trusted_file
from pyracast.regions.models import Region

FAILED = "Historical reference import failed:"


class Command(BaseCommand):
    help = (
        "Import an authorized historical Solcast export (JSON or CSV) for a plant's approved "
        "reference connection, through the same checks as the console upload. The file must "
        "lie inside the working directory."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument("--tenant-id", required=True, type=int, help="Cross-check.")
        parser.add_argument("--region-id", required=True, type=int)
        parser.add_argument("--file", required=True, type=Path)
        parser.add_argument("--source-timezone", help="Only for timestamps without an offset.")
        parser.add_argument("--period", choices=("PT30M", "PT60M"), help="Only when missing.")
        parser.add_argument("--reference", required=True, help="Authorization of the export.")
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Confirm that the file is historical model data for the plant's approved "
            "reference settings.",
        )

    def handle(self, *args: Any, **options: Any) -> str:
        if not options["confirm"]:
            raise CommandError(
                f"{FAILED} confirm with --confirm that the file is historical model data for "
                "the plant's approved reference settings."
            )
        path: Path = options["file"]
        try:
            content = read_trusted_file(
                path, suffixes=frozenset({".json", ".csv"}), trusted_root=Path.cwd()
            )
        except ValueError:
            raise CommandError(
                f"{FAILED} the file must be an existing CSV or JSON export inside the working "
                "directory."
            ) from None
        try:
            region = Region.objects.active().get(
                pk=options["region_id"], tenant_id=options["tenant_id"]
            )
            if region.approved_binding_id is None:
                raise ValidationError("The plant has no approved reference connection.")
            record, created = import_historical_upload(
                User.objects.get(username=options["username"]),
                region.tenant_id,
                region.pk,
                region.approved_binding_id,
                parse_historical_upload(
                    content,
                    path.name,
                    source_timezone=options["source_timezone"],
                    period=options["period"],
                ),
                path.name,
                settings_confirmed=True,
                source_reference=options["reference"],
            )
        except ValidationError as exc:
            raise CommandError(" ".join([FAILED, *exc.messages])) from exc
        except (ObjectDoesNotExist, PermissionDenied) as exc:
            raise CommandError(f"{FAILED} {exc}") from exc
        except OperationalError as exc:
            if retryable_database_code(exc) is None:
                raise
            raise CommandError(f"{FAILED} the plant is busy; retry.") from exc
        state = "imported" if created else f"already imported at {record.created_at.isoformat()}"
        return (
            f"Historical reference import {record.pk}: {record.interval_count} intervals; "
            f"SHA256 {record.content_sha256}; {state}."
        )
