# Hourly and pre-release database backups and restore rehearsal

A systemd timer on the production VM, and the release script before every migration, run `deploy/backup.py` to dump the `pyracast` PostgreSQL database, upload it to a private Cloud Storage bucket with a checksum check, and record a last-backup pointer that the watchdog reads. A weekly timer runs `deploy/rehearsal.py`, which restores the backup the pointer names, with its roles, into a throwaway PostgreSQL and proves that it serves. Failures are logged as `pyracast_backup_failed` (page the operator) or `pyracast_rehearsal_failed` (warning). Nothing in the web or API layers is involved, and no automatic restore of production exists.

## Entry points

- Timer: `pyracast-backup.timer` (`OnCalendar=hourly`, `RandomizedDelaySec=300`, `Persistent=true`) starts `pyracast-backup.service` (`deploy/systemd/`, `TimeoutStartSec=2400`, `PrivateTmp=true`, `SuccessExitStatus=75`). The unit runs `deploy/backup.py /etc/pyracast/host.json` only when that file exists; the script takes the release lock and checks `release-blocked` itself. Units are installed from `deploy/systemd/` by `deploy/install-units.sh`, run by each release and at boot.
- Release: `deploy/install-release.sh` runs `backup.py ... --release-dir "$release" --lock-fd 9 --kind prerelease` after draining the old release and before `up -d postgres` and `migrate`. It already holds the release lock (descriptor 9) and the `release-blocked` marker. Without a `--lock-fd` that names a descriptor holding the lock the script refuses with exit 2 and dumps nothing. On a first deployment the dump holds an empty schema.
- Rehearsal: `pyracast-rehearsal.timer` (`OnBootSec=1h`, Sundays 03:00, `RandomizedDelaySec=1800`, `Persistent=true`) starts `pyracast-rehearsal.service` (`Nice=19`, idle I/O, `TimeoutStartSec=3900`), which runs `deploy/rehearsal.py /etc/pyracast/host.json` (`main`, `rehearse`). An operator can run the same command as root; it is the only way to rehearse.
- Reuse: `deploy/smoke.sh` (CI and release workflows) calls `backup.store_backup` and `rehearsal.main` with the bucket replaced by a directory. `deploy/watchdog.py` imports `compose_command` and reads both records. `deploy/reset_database.py` imports `compose_command` and `upload_backup` (see `database-reset-and-restore`); `rehearsal.py` reuses its restore helpers. `backup_bucket` and `daily_backups` come from `infra/terraform/main.tf` (`google_storage_bucket.backup`) via `/etc/pyracast/host.json`.

## Sequence

```mermaid
sequenceDiagram
    participant T as timer or release script
    participant B as backup.py
    participant PG as postgres container
    participant G as Cloud Storage
    participant L as operations log
    participant W as watchdog
    participant X as rehearsal.py
    participant R as throwaway PostgreSQL
    T->>B: "--kind hourly or prerelease"
    opt hourly
        B->>B: "release lock, waits up to 600 s"
        alt lock busy or release-blocked
            B->>L: "WARNING pyracast_backup_skipped, exit 75"
        end
    end
    B->>PG: "one REPEATABLE READ psql session: table counts, then pg_dump zstd:3, timeout 1200 s"
    alt dump fails
        B->>L: pyracast_backup_failed
    else dump written
        loop up to 3 attempts, sleep 1 s then 2 s
            B->>G: "POST media, ifGenerationMatch=0, md5 sent and checked"
            alt 412 object exists
                B->>G: "GET metadata once, accept only equal md5 and size"
            end
        end
        B->>B: "prerelease: prerelease-backup.json; then replace last-backup.json atomically"
        B->>L: pyracast_backup_succeeded
    end
    W->>L: "pyracast_backup_failed when the pointer's mark is over 90 min of uptime old"
    X->>X: "rehearsal lock, pointer, sweep, capacity"
    X->>G: "GET object, size and md5 checked"
    X->>R: "docker run, network none, bootstrap.sql, pg_restore, bootstrap.sql"
    X->>R: "role check, row counts equal the dump's, migrate check, check_secrets in a probe"
    X->>L: "pyracast_rehearsal_passed or pyracast_rehearsal_failed, last-rehearsal.json"
    X->>R: "finally remove probe, container and volume"
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| all tables of database `pyracast` | R | `pg_dump` as `postgres` reads every table under one exported snapshot while the application may still write (hourly) or after drain (prerelease); the same transaction counts every table. Roles and tablespaces are not dumped. A dump stops below 5 percent free disk. The hourly dump lands in a `pyracast-backup-*` temporary directory removed on exit; the prerelease dump is kept in `/var/lib/pyracast/reset` until a release succeeds. |
| `postgres/YYYY/MM/DD/<stamp>-<kind>-<uuid>.dump` in the bucket | W, R | Never overwritten. The bucket keeps object versions and one lifecycle rule deletes every generation seven days after its creation, so a deleted or replaced dump stays recoverable until then. Bucket reads and writes are in the Cloud Storage Data Access audit log. |
| `daily/YYYY/MM/DD.dump` in the bucket | W | Only with host setting `daily_backups` true: the first hourly backup of a UTC day is copied there (kept 30 days by a rule that exists only with the daily tier). |
| `/var/lib/pyracast/last-backup.json` | W, R | `object`, `kind`, `size`, table `counts` and a clock mark (boot id and uptime). The rehearsal reads the pointer and the watchdog its mark. A prerelease backup also writes `<release>/prerelease-backup.json` (`object`, `counts`), which the installer reads through `deploy/release_record.py`. |
| `/var/lib/pyracast/last-rehearsal.json` | W, R | `status` `passed` (object, `recovery_seconds`, `tables`, `rows`, `secrets`) or `failed` (`step`), with a clock mark. |
| Cloud Logging `pyracast-operations` | W | `pyracast_backup_succeeded`, `_skipped`, `_failed`, `_daily_copied`, `_daily_copy_failed`, `pyracast_rehearsal_passed`, `_failed`, `pyracast_backup_failed` and `pyracast_rehearsal_unhealthy` from the watchdog. |
| every table | W, R | Only in the disposable restored database: `pg_restore` writes all tables, then roles, row counts, migrations and secrets are checked. |

## Authorization

- Host: the scripts run as root through systemd or the root-only release script. `host.json` is mode 0600, so the operator needs root: OS Login admin and the port-22 IAP tunnel on the VM (`google_compute_instance_iam_member.ssh_admin`, `ssh_tunnel`). The shared lock `/var/lib/pyracast/release.lock` is root only (mode 0600) and opened without following a link.
- Bucket: the runtime service account has only `roles/storage.objectCreator` and `objectViewer` on it (`infra/terraform/iam.tf`), so it can neither overwrite nor delete objects; the bucket enforces uniform access and public access prevention. The token comes from the metadata server (`deploy/operations_log.py` `access_token`). The bucket name must match a fixed pattern. Docker runs with argument lists, no shell. The throwaway database has no network, `no-new-privileges`, and PID, memory and CPU limits.

## Failure paths

| Condition | Result |
| --- | --- |
| Dump fails, times out, falls below the free-space floor or the compose call fails | The dump process is killed, the backup terminates its `pyracast-backup` database sessions and removes its files; `pyracast_backup_failed`, exit 1, nothing uploaded, no pointer. A release stops here with the application drained and `release-blocked` set. |
| Upload transport error, HTTP 408, 429 or 5xx, checksum or size mismatch, malformed response | Three attempts with 1 s and 2 s sleeps, then the original error, the failure event and exit 1; the dump is deleted. Other HTTP errors and programming errors are not retried. Cloud Storage validates the sent MD5. |
| 412 on upload | Object metadata is fetched once. Equal md5 and size count as success, otherwise `ValueError`. |
| Hourly run meets a busy lock for 600 s or `release-blocked` | `pyracast_backup_skipped` (`lock_timeout` or `release_blocked`), exit 75, which the unit counts as success. |
| Prerelease records cannot be written | The archive is deleted, the backup fails. |
| SIGTERM (unit timeout, stop) | Unwinds with cleanup and reports `pyracast_backup_failed` with `reason=terminated`. |
| Daily copy fails | Warning `pyracast_backup_daily_copy_failed`; the hourly backup still succeeded. |
| Pointer older than 90 minutes of uptime | Watchdog reports `pyracast_backup_failed` (`last_verified_backup_older_than_90_minutes`) while no release is blocked; a pointer of an earlier boot, or none, counts as old as this boot. |
| Invalid bucket name, unreadable host config or missing `backup_bucket` | `ValueError`, `OSError` or `KeyError`, same failure event and exit 1. Invalid command-line arguments exit 2 through `argparse` without an event. |
| Rehearsal lock held by another rehearsal for 5 s | "Another restore rehearsal holds its lock", exit 2, record unchanged. |
| A rehearsal step fails (`configuration`, `pointer`, `sweep`, `capacity`, `download`, `restore`, `roles`, `counts`, `migrations`, `secrets`) or SIGTERM arrives | Warning `pyracast_rehearsal_failed` with step and error type is reported before `last-rehearsal.json` records the failure; exit 1; probe, container and volume are removed. A run past its 3600 s deadline fails like any step. |
| No record, a failed record, or a record older than eight days of uptime | Watchdog warns `pyracast_rehearsal_unhealthy` (flow `host-supervision-and-alerting`). |

## Rules and invariants

- Release, watchdog recovery, `pyracast.service` stop and the hourly backup share `/var/lib/pyracast/release.lock`; the hourly backup holds it from the dump through upload and pointer, so the pointer names the newest dump. The prerelease dump never waits: its caller holds the lock. The release waits up to 300 s.
- Objects are written with `ifGenerationMatch=0` and a fresh uuid, so no backup can overwrite another. Success means the bucket returned the md5 and size of the local file; only then is the pointer replaced, from a uniquely named temporary file, from either kind.
- The rehearsal reads one release (`/opt/pyracast/current` resolved once) and the image its Compose configuration names, so it runs the same PostgreSQL build; it restores with owners and privileges, then proves roles (the runtime role cannot write `django_migrations`), equal row counts, `migrate --check` (`--plan` for a prerelease dump) and a decrypted stored secret. The probe shares the throwaway's network, labelled `pyracast.rehearsal` and named `<throwaway>-probe`; leftovers of a killed run are swept first.
- A rehearsal downloads within the object's size, the 5 percent free-space floor and its deadline, and starts only with memory and disk room beside the running site. While it holds `/var/lib/pyracast/rehearsal.lock`, `release_state.py prune` removes nothing.
- Every `pyracast_backup_failed` entry matches the `backup` log-based alert policy (`infra/terraform/monitoring.tf`), which emails the operator at most once per hour; rehearsal events are warnings without a policy.
- The recovery point is one hour while hourly backups succeed. There is no WAL archiving; objects are recoverable for seven days.

## Tests

- `tests/test_deployment_backup.py`: upload checksum, validated MD5, retry budget by status and error, 412 recovery, bounded commands (deadline and free-space floor), cleanup after an early exit, table counts in the exported snapshot, daily copy, refused command line, redacted logging.
- `tests/test_deployment_backup_lock.py`: lock held through the pointer, skip and exit 75, prerelease lock requirement, pointer content, release record, prerelease archive location, termination report.
- `tests/test_deployment_rehearsal.py`: restore with roles and recovery time, throwaway isolation, migration plan for a prerelease dump, failed-step record and cleanup, capacity, sweep, lock, deadline, single release, bounded download.
- `tests/test_operations_contracts.py`: units shipped by the release, rehearsal unit outlasts its deadline, hourly unit leaves lock and block to the script, exit 75 is success, helper lock mode, bucket versioning and audit log, daily tier setting.
- `deploy/test-release.sh` (CI, `backup.py` stubbed): backup position in the release order, failure modes, SIGKILL during backup. `deploy/smoke.sh` runs a real dump and rehearsal against Compose PostgreSQL with a directory as bucket. No Playwright coverage applies.
