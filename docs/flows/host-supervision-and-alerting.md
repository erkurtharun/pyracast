# Host watchdog, health probes and alerts

A systemd timer runs `deploy/watchdog.py` about once a minute on the production VM. Its independent steps check capacity, the age of the last backup and of the last restore rehearsal, restart failed Compose services, run the delivery health command, and check the origin certificate and origin pulls; each ends with a heartbeat event. The watchdog writes structured events to Cloud Logging. Cloud Monitoring turns those events into rate-limited operator emails, alerts when the heartbeat stops, and runs an independent HTTPS uptime check on `/health/ready/`.

## Entry points

- `pyracast-watchdog.timer` (`OnBootSec=180`, `OnUnitInactiveSec=60`) and `.service` (`TimeoutStartSec=300`) run `deploy/watchdog.py` `main`, which takes no arguments but `--help`. The unit files come from `deploy/systemd/` through `deploy/install-units.sh`, run by each release and at boot. `pyracast.service` (oneshot, `RemainAfterExit`) starts Compose and drains it on stop; only it and the Docker drop-in are written by the startup script `infra/terraform/templates/startup.sh.tftpl` on every VM boot.
- `GET /health/live/` and `GET /health/ready/`: `pyracast.health.live` and `pyracast.health.ready` in `pyracast.urls`. The Compose `api` healthcheck uses `live` (no database), so a database outage does not restart `api`; `deploy/install-release.sh`, `deploy/smoke.sh` and the uptime check use `ready`.
- `deploy/operations_log.py` `report` (the only event writer, also used by `deploy/backup.py`, `deploy/rehearsal.py` and `deploy/runtime_secrets.py`) and `infra/terraform/monitoring.tf` (notification channel, uptime check, readiness policy, six log-match operations policies, the heartbeat absence policy and seven security policies on the `pyracast.security` lines of the API log). No frontend screen.

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
        W->>L: "heartbeat state blocked, nothing else checked"
    else
        W->>L: "ERROR pyracast_backup_failed when the pointer's mark is over 90 min of uptime old"
        W->>L: "WARNING pyracast_rehearsal_unhealthy when failed or overdue"
        opt pyracast.service not active
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
            W->>L: "ERROR pyracast_runtime_unhealthy reason delivery_health"
        end
        W->>L: "WARNING pyracast_certificate_expiring, ERROR pyracast_origin_pull_unenforced"
        W->>L: "INFO pyracast_watchdog_heartbeat state ok, lock_busy or unit_failed"
    end
    L->>M: "log-match policy per event; absence policy on the heartbeat metric"
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
| `forecasts_dispatcherstatus` and the job, mail, account and region tables | R | Read by `check_delivery_health` (flow `delivery-health-check`); written by the dispatcher, not by this flow |
| `/var/lib/pyracast/release-blocked`, `last-backup.json`, `last-rehearsal.json`, `/proc/meminfo`, `/proc/uptime`, disk usage of `/` and `/var/lib/pyracast`, `deploy/release.env` of the current release | R | Release block, backup and rehearsal age (clock mark: boot id and uptime), capacity, site address and origin-pull mode |
| `/var/lib/pyracast/last-rehearsal.json` | W | Only when missing: `{"status": "scheduled", "mark": ...}`, linked so a record written meanwhile is never replaced |
| `/var/lib/pyracast/delivery-checked` | W | Touched before each delivery check |
| Cloud Logging log `pyracast-operations` | W | One `entries:write` per event (`gce_instance`, `jsonPayload.event`); the payload also goes to stderr |

## Authorization

- Probes are anonymous. Port 443 accepts only Cloudflare ranges, and the gateway routes `PathPrefix` `/health/` to `api` (`deploy/routes.yml`). `require_safe` allows GET and HEAD only (others 405). `SECURE_REDIRECT_EXEMPT` lets the plain-HTTP Compose probe skip the HTTPS redirect.
- The watchdog runs as root under systemd and reaches the database only through `exec api` with the runtime role. `report` uses the VM service account token (`roles/logging.logWriter` in `infra/terraform/iam.tf`). Alert email goes to `var.acme_email`.

## Failure paths

| Condition | Result |
|---|---|
| Database unreachable | `ready` 503 `unavailable`, no detail, `no-store`; `api` stays live. A PostgreSQL container that is unhealthy is reported as `pyracast_runtime_unhealthy` and left to Docker; the uptime alert fires after 5 min |
| Application services not recovered | `pyracast_runtime_unhealthy` with the service states; retried next run |
| A step times out (the run shares one 210 s deadline) or raises | `pyracast_runtime_unhealthy` with `step=<name>` and `outcome=timeout` or `error` (and `error_type`); the other steps still run; exit 1. A run past `TimeoutStartSec=300` is killed by systemd with no heartbeat |
| `check_delivery_health` exits non-zero | `pyracast_runtime_unhealthy` with `reason=delivery_health`, output discarded, no remediation |
| Release lock busy during a restart | Heartbeat state `lock_busy`; nothing restarted this run |
| `pyracast.service` not active | Heartbeat state `unit_failed` with `unit_state`; no further check, nothing restarted |
| Rehearsal record failed, or older than eight days of uptime (a host without one starts the window at the first check) | `pyracast_rehearsal_unhealthy` (warning) with `state` `failed` and `step`, or `overdue` |
| Certificate below a quarter of its lifetime (read on loopback port 8443) | `pyracast_certificate_expiring` with `days_remaining` |
| Origin pulls required and the host's port 443 accepts a handshake without the edge's certificate | `pyracast_origin_pull_unenforced` |
| Metadata server or Logging API unreachable | stderr `pyracast_operations_log_failed`, the alert is lost, the run continues. Other exceptions from `report` propagate |

## Rules and invariants

- One lock, `/var/lib/pyracast/release.lock`. The watchdog takes it without waiting and only to restart application containers; the hourly backup waits for it up to 600 s; `install-release.sh` waits 300 s and `ExecStop` waits for it, then drains with `compose stop --timeout 660` inside `TimeoutStopSec=960`.
- Only `pyracast.service` carries `ConditionPathExists=!release-blocked`; the watchdog and backup units run while a release is blocked, and the watchdog then checks capacity and sends the heartbeat only. The watchdog never recovers while blocked, stopped or locked.
- Before recovery `starting` counts as healthy, afterwards only `running` and `healthy`; `restarting` counts as failed. `SERVICES` is a fixed list (PostgreSQL, RabbitMQ and the application services including `maintenance`) without `migrate`. Failed existing application containers are restarted first to reset the `on-failure:5` budget, then `up -d --no-deps` starts only the failed services, so a stopped PostgreSQL or RabbitMQ stays stopped and only missing containers are recreated.
- Backup age and rehearsal age are uptime since the pointer's mark; a pointer of an earlier boot, or none, counts as old as this boot.
- Event names are the contract with `monitoring.tf`: one notification per 3600 s per policy, auto-close after 86400 s, while events repeat every minute. `pyracast_runtime_restarted`, `pyracast_rehearsal_unhealthy`, `pyracast_backup_skipped` and `pyracast_backup_succeeded` match no policy. The heartbeat feeds a log metric whose policy alerts when none arrives for fifteen minutes. The readiness policy counts failing checker locations (more than 1 for 300 s).

## Tests

- `tests/test_health.py` (live, ready, 503 body, 405), `tests/test_deployment_watchdog.py` (capacity thresholds, recovery only under the lock with the marker and unit checked again inside it, blocked and inactive-unit heartbeats, only failed services started, step timeouts and the shared deadline, delivery interval, backup and rehearsal age in uptime, certificate, origin pull), `tests/test_deployment_backup.py` (`report` fallback and propagation), `deploy/smoke.sh` (`ready` through the gateway), `deploy/test-release.sh` (installer lock wait and readiness gate).
- `tests/test_operations_contracts.py`: `watchdog.SERVICES` equals the long-running Compose services; every `monitoring.tf` policy event is reported by a host script and every `ERROR` event has a policy; the `pyracast.service` stop timeouts cover the Compose stop grace periods; the watchdog unit outlasts its deadline and runs while a release is blocked; a silent watchdog is alerted; `api` restarts on liveness.
