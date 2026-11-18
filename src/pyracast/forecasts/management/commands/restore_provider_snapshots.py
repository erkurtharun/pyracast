import logging
import sys
from pathlib import Path
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.core.management.base import BaseCommand, CommandError, CommandParser

from pyracast.background.failures import failure_origin, log_failure
from pyracast.forecasts.snapshot_restore import (
    EndpointSummary,
    Outcome,
    RestoreRefusedError,
    RestoreSummary,
    restore_provider_snapshots,
)

logger = logging.getLogger(__name__)

# Output names no path, exception message or export value: each can hold the site id.
REFUSALS: tuple[tuple[type[Exception], str], ...] = (
    (User.DoesNotExist, "The user does not exist."),
    (ObjectDoesNotExist, "The region or binding does not exist in that tenant."),
    (PermissionDenied, "The user is not a platform administrator."),
    (OSError, "The export cannot be read."),
)

REMEDIES = {
    "historical_import": "import its source file again",
    "scaled_forecasts": "normalize the reference capacity again",
    "scaled_estimated_actuals": "normalize the reference capacity again",
}
PHRASES = {
    Outcome.ALREADY_RESTORED: "already restored",
    Outcome.SLOT_TAKEN: "skipped, slot held by another collection",
    Outcome.OTHER_PROVIDER: "skipped, another provider",
}


def _phrase(endpoint: str, outcome: Outcome, count: int, *, dry_run: bool) -> str:
    if outcome == Outcome.RESTORED:
        text = "to restore" if dry_run else "restored"
    elif outcome == Outcome.NOT_LIVE:
        text = f"skipped, not a live collection ({REMEDIES[endpoint]})"
    else:
        text = PHRASES[outcome]
    return f"{count} {text}"


def _endpoint_line(item: EndpointSummary, *, dry_run: bool) -> str:
    phrases = ", ".join(
        _phrase(item.endpoint, outcome, count, dry_run=dry_run) for outcome, count in item.outcomes
    )
    period = (
        ""
        if item.period is None
        else f"; covers {item.period[0].isoformat()} to {item.period[1].isoformat()}"
    )
    return f"{item.endpoint}: {phrases}{period}."


def _refusal(error: Exception) -> str:
    if isinstance(error, RestoreRefusedError):
        return " ".join(error.messages)
    return next(sentence for kind, sentence in REFUSALS if isinstance(error, kind))


def _report(summary: RestoreSummary) -> list[str]:
    lines = [
        f"Provider snapshot restore for binding {summary.binding_id}: {summary.rows} export "
        f"rows, SHA-256 {summary.export_sha256}.",
        *(_endpoint_line(item, dry_run=summary.dry_run) for item in summary.endpoints),
    ]
    if summary.days:
        lines.append(f"Native estimated actuals by local day ({summary.timezone}):")
        lines.extend(f"{day.isoformat()} {state}" for day, state in summary.days)
    if summary.dry_run:
        lines.append("Dry run: nothing was written.")
    elif summary.restored:
        lines.append(f"Restored {summary.restored} snapshots.")
    else:
        lines.append("Nothing to restore.")
    return lines


class Command(BaseCommand):
    help = (
        "Restore the live provider snapshots of a database reset export into the binding "
        "that holds the same provider site."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument("--tenant-id", required=True, type=int)
        parser.add_argument("--region-id", required=True, type=int)
        parser.add_argument("--binding-id", required=True, type=int)
        parser.add_argument(
            "--export",
            required=True,
            help="Path of the export (plain or gzip JSON lines), or - for standard input.",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> str:
        binding_id = options["binding_id"]
        try:
            actor = User.objects.get(username=options["username"])
            export = options["export"]
            content = sys.stdin.buffer.read() if export == "-" else Path(export).read_bytes()
            summary = restore_provider_snapshots(
                actor,
                options["tenant_id"],
                options["region_id"],
                binding_id,
                content,
                dry_run=options["dry_run"],
            )
        except (ObjectDoesNotExist, PermissionDenied, RestoreRefusedError, OSError) as exc:
            logger.warning(
                "provider_snapshot_restore_refused binding_id=%s error_type=%s",
                binding_id,
                type(exc).__name__,
            )
            raise CommandError(f"Provider snapshot restore failed: {_refusal(exc)}") from None
        except Exception as exc:
            log_failure(logger, "provider_snapshot_restore_failed", exc, binding_id=binding_id)
            raise CommandError(
                "Provider snapshot restore failed unexpectedly: "
                f"{type(exc).__name__} at {failure_origin(exc)}."
            ) from None
        logger.info(
            "provider_snapshot_restore_finished binding_id=%s dry_run=%s restored=%s skipped=%s "
            "export_sha256=%s",
            binding_id,
            summary.dry_run,
            summary.restored,
            summary.rows - summary.restored,
            summary.export_sha256,
        )
        return "\n".join(_report(summary))
