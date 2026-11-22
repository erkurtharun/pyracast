# Host watchdog, health probes and alerts

A systemd timer runs `deploy/watchdog.py` about once a minute on the production VM. Its independent steps check capacity, how long a release has blocked the site, the age of the last backup and of the last restore rehearsal, a stopped `pyracast.service`, restart failed Compose services, run the delivery health command, and check the origin certificate and origin pulls; each run ends with a heartbeat event. Every event the host scripts write to Cloud Logging is declared once in `deploy/operations_events.py` with its severity and how it alerts. Cloud Monitoring turns those events into rate-limited operator emails, alerts on repeated restarts and when the heartbeat stops, and runs an independent HTTPS uptime check on `/health/ready/`.

## Entry points

- `pyracast-watchdog.timer` and `.service` run `deploy/watchdog.py` `main`. The unit files come from `deploy/systemd/` through `deploy/install-units.sh`. `pyracast.service` (oneshot, `RemainAfterExit`) starts Compose and drains it on stop; only it and the Docker drop-in are written by `infra/terraform/templates/startup.sh.tftpl` on every VM boot.
- `GET /health/live/` and `GET /health/ready/`: `pyracast.health.live` and `pyracast.health.ready` in `pyracast.urls`. The Compose `api` healthcheck uses `live` (no database); `deploy/install-release.sh`, `deploy/smoke.sh` and the uptime check use `ready`.
- `deploy/operations_log.py` `report` (the only event writer, shared with `deploy/backup.py` and `deploy/rehearsal.py`) and `infra/terraform/monitoring.tf` (notification channel, uptime check, readiness, log-match, repeated-restart, heartbeat-absence and security policies). No frontend screen.
- `deploy/operations_events.py` `EVENTS` (each event's severity, alert kind and sample fields) and `main`: `--test-alert EVENT` writes one sample line with `test: true` and changes nothing else, exit 1 when the line could not be written. Each event's runbook row is under "Alert events" in `docs/DEPLOYMENT.md`.

## Sequence

```mermaid
sequenceDiagram
    participant T as Timer
    participant W as watchdog.py
    participant C as docker compose
    participant L as Cloud Logging
    participant M as Cloud Monitoring
    participant H as health.ready
    participant O as Operator
    T->>W: "180 s after boot, then 60 s after each run ends"
    W->>L: "WARNING pyracast_host_capacity when disk free under 15 percent or memory under 10 percent"
    alt release-blocked exists
        W->>L: "WARNING pyracast_release_blocked with hours, when blocked over 1 h"
        W->>L: "heartbeat state blocked, nothing else checked"
    else
        W->>L: "ERROR pyracast_backup_failed when the pointer's mark is over 90 min of uptime old"
        W->>L: "WARNING pyracast_rehearsal_unhealthy when failed or overdue"
        opt pyracast.service not active
            W->>L: "ERROR pyracast_runtime_unhealthy unit_state when failed or inactive, lock free, no marker"
            W->>L: "heartbeat state unit_failed, nothing more checked"
        end
        W->>C: "ps --all --quiet, then docker inspect"
        opt an application service is not running or healthy, or missing
            W->>C: "release lock without waiting, restart failed, then up --no-deps --wait"
            alt still not healthy
                W->>L: "ERROR pyracast_runtime_unhealthy with the states"
            else recovered
                W->>L: "WARNING pyracast_runtime_restarted"
            end
        end
        W->>C: "exec -T api pyracast check_delivery_health, at most every 5 min"
        opt exit code not zero
            W->>L: "ERROR pyracast_delivery_unhealthy with alarms, phases, broker and queues"
        end
        W->>L: "WARNING pyracast_certificate_expiring, ERROR pyracast_origin_pull_unenforced"
        W->>L: "INFO pyracast_watchdog_heartbeat state ok, lock_busy or unit_failed"
    end
    L->>M: "log-match policy per alerted event; threshold on restarts; absence on heartbeats"
    M->>O: "email, at most one per hour per policy"
    M->>H: "GET /health/ready/ via Cloudflare and gateway, every 5 min"
    alt SELECT 1 raises DatabaseError
        H-->>M: 503 unavailable
        M->>O: "email when more than 1 checker location fails for 5 min"
    else ok
        H-->>M: 200 ok
    end
```

## Data

| Table or file | R/W | What |
|---|---|---|
| `forecasts_dispatcherstatus` and the job, mail, account and region tables | R | Read by `check_delivery_health` (flow `delivery-health-check`) |
| `/var/lib/pyracast/release-blocked`, `last-backup.json`, `last-rehearsal.json`, `/proc/meminfo`, `/proc/uptime`, disk usage, `deploy/release.env` | R | Release block and its `started_at`, backup and rehearsal age, capacity, site address and origin-pull mode |
| `/var/lib/pyracast/last-rehearsal.json` | W | Only when missing: `{"status": "scheduled", "mark": ...}`; a record written meanwhile is never replaced |
| `/var/lib/pyracast/delivery-checked` | W | Touched before each delivery check |
| Cloud Logging log `pyracast-operations` | W | One `entries:write` per event (`gce_instance`, `jsonPayload.event`, `test` on a test alert); the payload also goes to stderr |
| Log-based metrics `watchdog_heartbeat`, `runtime_restarts` | W | Count heartbeat and `pyracast_runtime_restarted` lines, test lines left out |

## Authorization

- Probes are anonymous. Port 443 accepts only Cloudflare ranges, and the gateway routes `PathPrefix` `/health/` to `api` (`deploy/routes.yml`). `require_safe` allows GET and HEAD only (others 405). `SECURE_REDIRECT_EXEMPT` lets the plain-HTTP Compose probe skip the HTTPS redirect.
- The watchdog runs as root under systemd and reaches the database only through `exec api` with the runtime role. `report` uses the VM service account token (`roles/logging.logWriter` in `infra/terraform/iam.tf`). Alert email goes to `var.acme_email`.

## Failure paths

| Condition | Result |
|---|---|
| Database unreachable | `ready` 503 `unavailable`, no detail, `no-store`; `api` stays live; the uptime alert fires after 5 min. An unhealthy PostgreSQL container is reported as `pyracast_runtime_unhealthy` and left to Docker |
| Application services not recovered | `pyracast_runtime_unhealthy` with `services`; retried next run |
| Services recovered by a restart in more than two runs within an hour | `pyracast_runtime_restarted` each run; the `runtime_restarts` policy alerts on the total |
| `pyracast.service` `failed` or `inactive` while the release lock is free and no marker exists | `pyracast_runtime_unhealthy` with `unit_state`; an `activating` unit, or one a release or stop holds the lock for, is not reported |
| Marker present for more than an hour | `pyracast_release_blocked` with `hours`, counted from the `started_at` the installer recorded in the marker, else from the marker's file time |
| A step times out (the run shares one deadline) or raises | `pyracast_watchdog_step_failed` with `step`, `outcome` `timeout` or `error`, and `error_type`; the other steps still run; exit 1. A run past `TimeoutStartSec=300` is killed by systemd with no heartbeat |
| `check_delivery_health` exits non-zero | `pyracast_delivery_unhealthy` with `alarms`, `dispatch_phases`, `broker_unreachable` and `missing_queues` read from its stdout; stdout that is not such a report gives `outcome: unreadable` with `exit_status`; no remediation |
| Rehearsal record failed, or older than eight days of uptime | `pyracast_rehearsal_unhealthy` (warning) with `state` `failed` or `overdue` |
| Certificate below a quarter of its lifetime | `pyracast_certificate_expiring` with `days_remaining`; the probe requires TLS 1.2 or later |
| Origin pulls required and the host's port 443 accepts a handshake without the edge's certificate | `pyracast_origin_pull_unenforced` |
| Metadata server or Logging API unreachable | stderr `pyracast_operations_log_failed`, the alert is lost, the run continues. Other exceptions from `report` propagate |

## Rules and invariants

- One lock, `/var/lib/pyracast/release.lock`. The watchdog takes it without waiting, to read the unit state of a stopped `pyracast.service` and to restart containers; the hourly backup waits for it up to 600 s; `install-release.sh` waits 300 s and `ExecStop` waits for it, then drains within `TimeoutStopSec=960`.
- Only `pyracast.service` carries `ConditionPathExists=!release-blocked`; while a release is blocked the watchdog checks capacity and sends the heartbeat only. It never recovers while blocked, stopped (heartbeat `unit_failed`) or locked (`lock_busy`).
- Before recovery `starting` counts as healthy, afterwards only `running` and `healthy`; `restarting` counts as failed. `SERVICES` is a fixed list without `migrate`. Failed existing application containers are restarted first to reset the `on-failure:5` budget, then `up -d --no-deps` starts only the failed services, so a stopped PostgreSQL or RabbitMQ stays stopped.
- Backup and rehearsal age are uptime since the pointer's mark; a pointer of an earlier boot, or none, counts as old as this boot.
- `EVENTS` is the contract with `monitoring.tf`. A `match` event has its own log-match policy in `local.operations_alerts` (one notification per 3600 s); the `rate` event `pyracast_runtime_restarted` alerts above two non-test lines in a 3660 s window; a `Silent` event says why it needs none (the heartbeat, successes, `pyracast_backup_skipped`, and `pyracast_rehearsal_failed`, which the watchdog reports as `pyracast_rehearsal_unhealthy`). Each policy's text names the event and its runbook row.
- Threshold policies (repeated restarts over 3660 s, each security threshold over 660 s: the hour or ten minutes plus the 60 s the condition must hold) evaluate missing data as not met and auto-close after 3660 s, so an incident closes once its window holds no line and a later burst notifies again. The heartbeat policy alerts when no non-test heartbeat arrives for fifteen minutes; the readiness policy counts failing checker locations (more than 1 for 300 s).

## Tests

- `tests/test_health.py`, `tests/test_deployment_watchdog.py` (recovery only under the lock, blocked and stopped-unit reports, release block age, delivery report fields, step failures, the TLS minimum, backup and rehearsal age), `tests/test_deployment_backup.py`.
- `tests/test_operations_events.py` (test alerts, every written event declared) and `tests/test_operations_contracts.py` (`watchdog.SERVICES` equals the long-running Compose services; policies match the alert kinds of `EVENTS`).
