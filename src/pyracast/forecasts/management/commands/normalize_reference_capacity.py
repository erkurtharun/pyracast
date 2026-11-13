from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from pyracast.forecasts.capacity_normalization import normalize_reference_capacity


class Command(BaseCommand):
    help = "Normalize authorized historical reference snapshots to a target binding capacity."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument("--tenant-id", required=True, type=int)
        parser.add_argument("--region-id", required=True, type=int)
        parser.add_argument("--source-binding-id", required=True, type=int)
        parser.add_argument("--target-binding-id", required=True, type=int)
        parser.add_argument("--reference", required=True)

    def handle(self, *args: Any, **options: Any) -> str:
        try:
            actor = User.objects.get(
                username=options["username"], is_active=True, is_superuser=True
            )
            snapshot_ids = normalize_reference_capacity(
                actor,
                options["tenant_id"],
                options["region_id"],
                options["source_binding_id"],
                options["target_binding_id"],
                options["reference"],
            )
        except (ObjectDoesNotExist, PermissionDenied, ValidationError) as exc:
            raise CommandError("Reference capacity normalization failed: " + str(exc)) from exc
        return f"Reference capacity normalization: {len(snapshot_ids)} snapshots"
