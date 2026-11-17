# Database reset and restore

The owner runs `deploy/reset_database.py` as root on the production VM to take the site down with a verified pre-reset archive (`prepare`), to bring the previous release back (`abort`), to replace the database with an empty, provisioned one (`reset`), or to bring the database back from a verified archive and start the previous release (`restore`). It is the procedure of the one-time production reset and the recovery of a release that fails after maintenance began. Each destructive step refuses unless the step before it verifiably succeeded. Nothing in the web or API layers is involved.

## Entry points

- Operator: `python3 /opt/pyracast/current/deploy/reset_database.py /etc/pyracast/host.json {prepare | abort | reset --confirm OBJECT | restore --confirm OBJECT}` (`deploy/reset_database.py` `main`). `host.json` supplies `backup_bucket` and `runtime_secret_version` (`load_host`); the release directory is `/opt/pyracast/current`.
- Reuse: `compose_command` and `upload_backup` from `deploy/backup.py`, `access_token` from `deploy/operations_log.py`; `abort`, `reset` and `restore` render the environment files with `deploy/runtime_secrets.py` of `/opt/pyracast/current`.
- Smoke: `deploy/smoke.sh` runs `main` through a driver that replaces only the bucket upload, the runtime secrets, the public readiness probe, `systemctl` and, where `fcntl` is missing, the host lock.

## Sequence

```mermaid
sequenceDiagram
    participant O as owner
    participant T as reset_database.py
    participant H as host files
    participant PG as postgres container
    participant MQ as rabbitmq container
    participant R as throwaway PostgreSQL
    participant G as Cloud Storage
    O->>T: prepare
    T->>H: "take release lock, refuse if marker exists"
    T->>T: "curl /health/ready/ must answer ok"
    T->>H: "write release-blocked holding a new session identity, sync"
    T->>PG: "stop gateway api web, dispatcher, worker notifications"
    T->>PG: "pg_dump into /var/lib/pyracast/reset/"
    T->>G: "upload postgres/(stamp)-prereset-(uuid).dump"
    T->>PG: "row count of every public table"
    T->>R: "pg_restore --no-owner --no-acl, row counts, remove"
    T->>H: "record object, SHA-256, counts; session.json with identity and object"
    O->>T: "reset --confirm OBJECT"
    T->>H: "marker, current session names OBJECT, record, SHA-256 of the kept archive"
    T->>MQ: "purge every queue, verify zero messages"
    T->>H: "mark the session replaced, sync"
    T->>PG: "DROP DATABASE WITH (FORCE), CREATE DATABASE, bootstrap.sql without roles, role checks"
    O->>T: "restore --confirm OBJECT"
    alt no record for OBJECT
        T->>G: "download, compare MD5 and size"
        T->>R: "verify and count"
    end
    T->>MQ: "purge every queue"
    T->>H: "session replaced with OBJECT, sync"
    T->>PG: "recreate, bootstrap, pg_restore with owners and ACLs, compare counts, role checks, migrate --check"
    T->>PG: "compose up --detach --wait, curl readiness"
    T->>H: "remove release-blocked, start pyracast.service, remove the session"
    O->>T: abort
    T->>PG: "migrate --check, compose up --detach --wait, curl readiness"
    T->>H: "remove release-blocked, start pyracast.service, remove the session"
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| all tables of database `pyracast` | R, W | `prepare` dumps and counts them; `reset` drops the database; `restore` recreates it from the archive with owners and privileges and compares each table's row count with the record |
| database-level grants, schema `public`, `btree_gist`, default privileges | W | The database part of `deploy/bootstrap.sql`, applied by `reset` and `restore`; roles and their memberships belong to the cluster and survive the drop |
| `postgres/<stamp>-prereset-<uuid>.dump` in the backup bucket | W, R | Uploaded by `prepare`; `restore` downloads an object it has no record for |
| `/var/lib/pyracast/reset/<object>.dump` and `<object>.json` | W, R | The kept archive and its record: object name, SHA-256, row count per table |
| `/var/lib/pyracast/reset/session.json` | W, R | Marker identity, object, release commit and whether the database was replaced (synced before `DROP DATABASE`). Current only while the marker holds the same identity; removed by `abort` and `restore` |
| `/var/lib/pyracast/release-blocked` | W, R | Written with a session identity and synced by `prepare` (`restore` adds one to an empty marker); the installer keeps its content and removes it on success; removed by `abort` and `restore` after readiness |
| RabbitMQ queues | W | Purged by `reset` and `restore`, dead-letter queues included |

## Authorization

- Host: root on the VM (OS Login admin through the IAP tunnel); `host.json` is mode 0600. The tool calls Docker, curl, `sync` and `systemctl` with argument lists, never a shell.
- Bucket: the VM's service account token from the metadata server; it can create and read objects, never overwrite or delete them. The object name must match `postgres/....dump`.
- Destructive steps need `--confirm` naming the archive of the current session (or, for `restore` without a reset in this maintenance, any verified archive with applied migrations), the marker, and no running, restarting or paused application container.

## Failure paths

| Condition | Result |
| --- | --- |
| Release lock held | "Refused: Another release, backup or watchdog operation holds the release lock...", exit 2 |
| `prepare` with the marker present or the site not ready | Refused before anything is written; with the marker it names `abort`, or `restore` of the session's object once `reset` replaced the database |
| `prepare` fails after the marker (stop, dump, upload, counts differ, throwaway restore) | Exit 1; the site stays down; the message names `abort`; no record is written, so `reset` refuses that object |
| `abort` after `reset` replaced the database in this maintenance | Refused, naming the `restore` command for the session's object |
| `abort` when `migrate --check` of the release fails on the database | Refused before the application starts |
| `abort` or `restore` readiness fails | Exit 1; the application is stopped again, marker kept; the same command can be run again |
| Interrupted (Ctrl-C) | Exit 130 with the command to run next |
| `reset` without a current session for that object (no `prepare`, an earlier maintenance, another prefix), with no marker, an application container running, a malformed object, a missing record, a missing or changed archive, or missing roles | Refused before any purge or drop |
| `restore` of another object once `reset` replaced the database, of an archive recorded for another object, or of an archive without applied migrations | Refused before any purge or drop |
| `restore` when `migrate --check` fails on the restored database | Exit 1; marker and session kept, release not started |
| A queue keeps messages after the purge | Exit 1 before the drop |
| Downloaded object differs from the bucket's MD5 or size | Refused, partial file removed, nothing dropped |
| Restored row counts differ from the record | Exit 1 with the differing tables; marker kept, release not started; `restore` can be run again |

## Rules and invariants

- Stop order equals the installer's: gateway, api, web (100 s), dispatcher (660 s), worker and notifications (660 s). PostgreSQL and RabbitMQ stay up.
- An archive is used only when its record names the confirmed object and its file still has the recorded SHA-256; a record is written only after a throwaway restore succeeded (and, in `prepare`, matched the live row counts).
- A session ends with its maintenance: the installer touches an existing marker without changing it and removes it on success (`deploy/test-release.sh` modes `maintenance-success` and `maintenance-migration`), so a later failed release cannot inherit a reset's refusal.
- Queues are purged before every drop: primary keys restart at 1, so a leftover message would address an unrelated row.
- The throwaway restore uses the PostgreSQL image of the current release, no network, and is removed in `finally`. The live restore keeps owners and ACLs.
- `abort` and `restore` start the release `/opt/pyracast/current` points to only after its `migrate --check` passes; the tool never changes that link or the installer.

## Tests

- `tests/test_deployment_reset.py` with a fake `subprocess`: stop order and marker before the stops, upload and kept archive, every refusal before a destructive call, the session across a successful and a failed later release, the session synced before the drop, count mismatch, lock refusal of every subcommand, verification cleanup and readiness wait, queue purge and drop order, bootstrap without role statements, restore with owners and its check order, download checksum, host restart for `abort`, `reset` and `restore` (Compose needs the environment files), the unit started after every `abort` and `restore`, readiness failure and retry, the command line's exit codes, interruption and next steps. `tests/test_deployment_helpers.py`: Python 3.12 syntax and `--help` without dependencies.
- `deploy/smoke.sh` (CI and Release): `prepare` then `abort` serves again; `prepare`, `reset`, a pre-release dump of the empty database, `migrate` from zero under the restricted owner and a start; an `abort` that is refused after the reset; the stop of a failed release, then `restore`: row counts, table owners, the runtime role's missing DDL right and a stored password are back and the site is ready.
- `deploy/test-release.sh`: the installer with a marker that holds a session succeeds and removes it, or fails in `migrate` and keeps its content.
- Gaps: the real bucket upload and download, the production readiness probe, `systemctl` and the release lock on the VM run first on the host; the proof of a restored sign-in is a stored password checked in the container, not an HTTP login (email verification and MFA). `dump_database` and `verify_archive` copy about 90 lines of `deploy/backup.py` (`create_backup`, `restore_archive`) to keep this package off the S list; folding them together belongs to P109, which changes `backup.py`. No Playwright coverage applies.
