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
class Silent:
    reason: str


@dataclass(frozen=True)
class Event:
    severity: Literal["INFO", "WARNING", "ERROR"]
    # How monitoring.tf notifies the operator: "match", a log-match policy on every line; "rate",
    # a threshold policy on the event's log-based metric, which leaves test lines out; Silent,
    # with why no line of the event needs a notification.
    alerts: Literal["match", "rate"] | Silent
    # The fields of one real line, written by the event's test alert.
    sample: Mapping[str, object]


# Every event the host scripts write to the pyracast-operations log; docs/DEPLOYMENT.md has
# the runbook entry of each.
EVENTS: Mapping[str, Event] = {
    "pyracast_watchdog_heartbeat": Event(
        "INFO", Silent("a heartbeat; its absence for fifteen minutes alerts"), {"state": "ok"}
    ),
    "pyracast_watchdog_step_failed": Event(
        "ERROR", "match", {"step": "delivery", "outcome": "timeout"}
    ),
    "pyracast_host_capacity": Event(
        "WARNING",
        "match",
        {
            "boot_disk_free_percent": 12.5,
            "data_disk_free_percent": 40.0,
            "memory_available_percent": 35.0,
        },
    ),
    "pyracast_runtime_unhealthy": Event("ERROR", "match", {"services": {"worker": "exited"}}),
    "pyracast_runtime_restarted": Event("WARNING", "rate", {"services": ["worker"]}),
    "pyracast_delivery_unhealthy": Event(
        "ERROR",
        "match",
        {
            "alarms": {"overdue_collections": 1},
            "dispatch_phases": [],
            "broker_unreachable": False,
            "missing_queues": [],
        },
    ),
    "pyracast_release_blocked": Event("WARNING", "match", {"hours": 2.0}),
    "pyracast_certificate_expiring": Event("WARNING", "match", {"days_remaining": 20.0}),
    "pyracast_origin_pull_unenforced": Event(
        "ERROR", "match", {"observation": "completed a handshake without a client certificate"}
    ),
    "pyracast_backup_succeeded": Event(
        "INFO",
        Silent("a success"),
        {
            "object": "postgres/2026/11/21/20261121T100000Z-hourly-0123456789abcdef.dump",
            "size": 1048576,
            "duration_seconds": 4.2,
        },
    ),
    "pyracast_backup_skipped": Event(
        "WARNING",
        Silent("a release holds the lock; a missing backup alerts as pyracast_backup_failed"),
        {"reason": "lock_timeout"},
    ),
    "pyracast_backup_failed": Event(
        "ERROR", "match", {"reason": "last_verified_backup_older_than_90_minutes"}
    ),
    "pyracast_backup_setting_invalid": Event("ERROR", "match", {"setting": "daily_backups"}),
    "pyracast_backup_daily_copied": Event(
        "INFO", Silent("a success"), {"object": "daily/2026/11/21.dump"}
    ),
    "pyracast_backup_daily_copy_failed": Event("WARNING", "match", {"error": "HTTPError"}),
    "pyracast_rehearsal_passed": Event(
        "INFO",
        Silent("a success"),
        {
            "object": "postgres/2026/11/21/20261121T100000Z-hourly-0123456789abcdef.dump",
            "recovery_seconds": 95.0,
            "tables": 51,
            "rows": 3364,
            "secrets": 2,
        },
    ),
    "pyracast_rehearsal_failed": Event(
        "WARNING",
        Silent("the watchdog reports it as pyracast_rehearsal_unhealthy, which alerts"),
        {"step": "counts", "error": "RefusedError"},
    ),
    "pyracast_rehearsal_unhealthy": Event("WARNING", "match", {"state": "overdue"}),
    "pyracast_secrets_failed": Event(
        "ERROR", "match", {"category": "unavailable", "secret": "host"}
    ),
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
