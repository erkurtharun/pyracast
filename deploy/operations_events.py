import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import operations_log


@dataclass(frozen=True)
class Event:
    severity: Literal["INFO", "WARNING", "ERROR"]
    # True: a log-match alert policy in monitoring.tf notifies the operator.
    alerts: bool
    # The fields of one real line, written by the event's test alert.
    sample: Mapping[str, object]


# Every event the host scripts write to the pyracast-operations log; docs/DEPLOYMENT.md has
# the runbook entry of each.
EVENTS: Mapping[str, Event] = {
    "pyracast_watchdog_heartbeat": Event("INFO", False, {"state": "ok"}),
    "pyracast_watchdog_step_failed": Event(
        "ERROR", True, {"step": "delivery", "outcome": "timeout"}
    ),
    "pyracast_host_capacity": Event(
        "WARNING",
        True,
        {
            "boot_disk_free_percent": 12.5,
            "data_disk_free_percent": 40.0,
            "memory_available_percent": 35.0,
        },
    ),
    "pyracast_runtime_unhealthy": Event("ERROR", True, {"services": {"worker": "exited"}}),
    "pyracast_runtime_restarted": Event("WARNING", False, {"services": ["worker"]}),
    "pyracast_delivery_unhealthy": Event(
        "ERROR",
        True,
        {
            "alarms": {"overdue_collections": 1},
            "dispatch_phases": [],
            "broker_unreachable": False,
            "missing_queues": [],
        },
    ),
    "pyracast_release_blocked": Event("WARNING", True, {"hours": 2.0}),
    "pyracast_certificate_expiring": Event("WARNING", True, {"days_remaining": 20.0}),
    "pyracast_origin_pull_unenforced": Event(
        "ERROR", True, {"observation": "completed a handshake without a client certificate"}
    ),
    "pyracast_backup_succeeded": Event(
        "INFO",
        False,
        {
            "object": "postgres/2026/11/21/20261121T100000Z-hourly-0123456789abcdef.dump",
            "size": 1048576,
            "duration_seconds": 4.2,
        },
    ),
    "pyracast_backup_skipped": Event("WARNING", False, {"reason": "lock_timeout"}),
    "pyracast_backup_failed": Event(
        "ERROR", True, {"reason": "last_verified_backup_older_than_90_minutes"}
    ),
    "pyracast_backup_setting_invalid": Event("ERROR", True, {"setting": "daily_backups"}),
    "pyracast_backup_daily_copied": Event("INFO", False, {"object": "daily/2026/11/21.dump"}),
    "pyracast_backup_daily_copy_failed": Event("WARNING", True, {"error": "HTTPError"}),
    "pyracast_rehearsal_passed": Event(
        "INFO",
        False,
        {
            "object": "postgres/2026/11/21/20261121T100000Z-hourly-0123456789abcdef.dump",
            "recovery_seconds": 95.0,
            "tables": 51,
            "rows": 3364,
            "secrets": 2,
        },
    ),
    "pyracast_rehearsal_failed": Event(
        "WARNING", False, {"step": "counts", "error": "RefusedError"}
    ),
    "pyracast_rehearsal_unhealthy": Event("WARNING", True, {"state": "overdue"}),
    "pyracast_secrets_failed": Event("ERROR", True, {"category": "unavailable", "secret": "host"}),
}


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write one operations event marked as a test, so its alert can be checked; "
        "nothing else on the host changes."
    )
    parser.add_argument("--test-alert", required=True, choices=sorted(EVENTS), metavar="EVENT")
    options = parser.parse_args(arguments)
    event = EVENTS[options.test_alert]
    written = operations_log.report(options.test_alert, event.severity, test=True, **event.sample)
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
