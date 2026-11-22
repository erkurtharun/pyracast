from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import IntegrityError, OperationalError

from pyracast.errors import retryable_database_code
from pyracast.forecasts.capacity_normalization import (
    NormalizationOutcome,
    normalize_reference_capacity,
)

FAILED = "Reference capacity normalization failed:"


def _report(outcome: NormalizationOutcome) -> str:
    endpoints = ", ".join(f"{name} {count}" for name, count in sorted(outcome.by_endpoint.items()))
    count = sum(outcome.by_endpoint.values())
    planned = f"{count} snapshots" + (f" ({endpoints})" if endpoints else "")
    if outcome.first_target_date is not None:
        planned += f" for {outcome.first_target_date} to {outcome.last_target_date}"
    lines = [
        f"Source binding {outcome.source_binding_id}: {outcome.source_capacity_kw:g} kW; "
        f"target binding {outcome.target_binding_id}: {outcome.target_capacity_kw:g} kW; "
        f"factor {outcome.factor:g}.",
        f"Dry run: would create {planned}; reused {len(outcome.reused)}; nothing was written."
        if outcome.dry_run
        else f"Created {planned}; reused {len(outcome.reused)}.",
    ]
    lines += [
        f"Binding {binding_id} holds copies in the source history; normalize it to the "
        "target separately."
        for binding_id in outcome.predecessors
    ]
    return "\n".join(lines)


class Command(BaseCommand):
    help = (
        "Copy a binding's archived actuals and forecasts onto another binding of the same plant "
        "that differs only in its declared reference capacity, scaled to that capacity. The "
        "target defaults to the plant's approved binding; a repeated run writes nothing new."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument(
            "--tenant-id", required=True, type=int, help="Cross-check of the plant."
        )
        parser.add_argument("--region-id", required=True, type=int)
        parser.add_argument("--source-binding-id", required=True, type=int)
        parser.add_argument("--target-binding-id", type=int)
        parser.add_argument(
            "--reference", required=True, help="Authorization of the capacity-only change."
        )
        parser.add_argument("--dry-run", action="store_true", help="Report the plan only.")

    def handle(self, *args: Any, **options: Any) -> str:
        try:
            outcome = normalize_reference_capacity(
                User.objects.get(username=options["username"]),
                options["tenant_id"],
                options["region_id"],
                options["source_binding_id"],
                options["target_binding_id"],
                options["reference"],
                dry_run=options["dry_run"],
            )
        except ValidationError as exc:
            raise CommandError("\n".join([FAILED, *exc.messages])) from exc
        except (ObjectDoesNotExist, PermissionDenied) as exc:
            raise CommandError(f"{FAILED} {exc}") from exc
        except IntegrityError as exc:
            raise CommandError(f"{FAILED} a stored copy conflicts; nothing was written.") from exc
        except OperationalError as exc:
            if retryable_database_code(exc) is None:
                raise
            raise CommandError(f"{FAILED} Region busy, retry.") from exc
        return _report(outcome)
