# Database reset and restore

The owner runs `deploy/reset_database.py` as root on the production VM to take the site down with a verified pre-reset archive and Solcast export (`prepare`), to bring the previous release back (`abort`), to replace the database with an empty, provisioned one (`reset`), or to bring the database back from a verified archive and start the previous release (`restore`). It is the procedure of the one-time production reset and the recovery of a release that fails after maintenance began. Each destructive step refuses unless the step before it verifiably succeeded. Nothing in the web or API layers is involved.

## Entry points

- Operator: `python3 /opt/pyracast/current/deploy/reset_database.py /etc/pyracast/host.json {prepare | abort | reset --confirm OBJECT | restore --confirm OBJECT}` (`deploy/reset_database.py` `main`). `host.json` supplies `backup_bucket` and `runtime_secret_version` (`load_host`); the release directory is `/opt/pyracast/current`.
- Reuse: `compose_command` and `upload_backup` from `deploy/backup.py`, `access_token` from `deploy/operations_log.py`; `abort`, `reset` and `restore` render the environment files with `deploy/runtime_secrets.py` of `/opt/pyracast/current`. `deploy/rehearsal.py` reuses the throwaway-restore and count helpers; `deploy/release_record.py` (run by `deploy/install-release.sh`) records a release's pre-release dump as the session's recovery point and prints the recovery commands.
- Follow-up: after the release, `pyracast restore_provider_snapshots` reads the Solcast export back as native succeeded collections of the recreated binding (flow `provider-snapshot-restore`; the procedure is in `docs/DEPLOYMENT.md`).
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
    T->>H: "take release lock without waiting, refuse if marker exists"
    T->>T: "curl loopback /health/ready/ must answer ok"
    T->>H: "write release-blocked holding a new session identity, sync"
    T->>PG: "stop dispatcher, worker and notifications, api and web"
    T->>PG: "pg_dump into /var/lib/pyracast/reset/"
    T->>G: "upload postgres/(stamp)-prereset-(uuid).dump"
    T->>PG: "row count of every public table"
    T->>R: "pg_restore --no-owner --no-acl, row counts, remove"
    T->>PG: "COPY every Solcast snapshot with job, binding and plant ids, gzip, count check"
    T->>G: "upload the Solcast export"
    T->>H: "record object, SHA-256, counts, export; session.json with identity and object"
    O->>T: "reset --confirm OBJECT"
    T->>H: "marker, current session names OBJECT, record, SHA-256 of the archive and the export"
    T->>MQ: "purge every queue, verify zero messages"
    T->>H: "mark the session replaced, sync"
    T->>PG: "DROP DATABASE WITH (FORCE), CREATE DATABASE, bootstrap.sql, role checks"
    O->>T: "restore --confirm OBJECT"
    alt no record for OBJECT
        T->>G: "download archive (and export), compare MD5 and size"
        T->>R: "verify and count"
    end
    T->>MQ: "purge every queue"
    T->>H: "session replaced with OBJECT, sync"
    T->>PG: "recreate, bootstrap, pg_restore with owners and ACLs, bootstrap again, compare counts, role checks, migrate --check"
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
| `forecasts_forecastsnapshot`, `forecasts_collectionjob`, `connections_sitebinding`, `connections_provideraccount`, `plants_plantconfiguration` | R | `prepare` exports every Solcast snapshot with its job row, binding row and plant ids through `psql` `COPY` |
| roles, database grants, extensions, schema `public`, default privileges, the migration ledger's read-only grant | W | `deploy/bootstrap.sql`, applied whole in one transaction by `reset` after the create and by `restore` after the create and again after `pg_restore`; roles belong to the cluster and survive the drop |
| `postgres/<stamp>-prereset-<uuid>.dump` and `.solcast.jsonl.gz` in the backup bucket | W, R | Uploaded by `prepare`; `restore` downloads an object (and a pre-reset archive's export) it has no record for |
| `/var/lib/pyracast/reset/<object>.dump`, `<object>.json` and the export | W, R | The kept archive, its record (object name, SHA-256, row count per table, export object, SHA-256 and row count) and the export |
| `/var/lib/pyracast/reset/session.json` | W, R | Marker identity, object, release commit and whether the database was replaced (synced before `DROP DATABASE`; the installer records it through `release_record.py` right before `migrate`). Current only while the marker holds the same identity; removed by `abort`, `restore` and a successful release, which also removes the `-prerelease-` files |
| `/var/lib/pyracast/release-blocked` | W, R | Written with a session identity and synced by `prepare` (`restore` adds one to an empty marker); the installer keeps an existing marker's content and file time (the watchdog counts a block from that file time) and removes it on success; removed by `abort` and `restore` after readiness |
| RabbitMQ queues | W | Purged by `reset` and `restore`, dead-letter queues included |

## Authorization

- Host: root on the VM (OS Login admin through the IAP tunnel); `host.json` is mode 0600. The tool calls Docker, curl, gzip, `sync` and `systemctl` with argument lists, never a shell.
- Bucket: the VM's service account token from the metadata server; it can create and read objects, never overwrite or delete them. The object name must match `postgres/....dump`.
- Destructive steps need `--confirm` naming the archive of the current session (or, for `restore` while the database is unchanged in this maintenance, any verified archive with applied migrations), the marker, and no running, restarting or paused application container. `reset` and a `restore` of a pre-reset archive also need its Solcast export with its recorded SHA-256.

## Failure paths

| Condition | Result |
| --- | --- |
| Release lock held | "Refused: Another release, backup or watchdog operation holds the release lock...", exit 2 |
| `prepare` with the marker present or the site not ready | Refused before anything is written; with the marker it names `abort`, or `restore` of the session's object once the database changed in this maintenance |
| `prepare` fails after the marker (stop, dump, upload, counts differ, throwaway restore, export count or upload) | Exit 1; the site stays down; the message names `abort`; no record is written, so `reset` refuses that object |
| `abort` once the database changed in this maintenance (`reset`, `restore` or a release's `migrate`) | Refused, naming the `restore` command for the session's object |
| `abort` when `migrate --check` of the release fails on the database | Refused before the application starts |
| `abort` without a maintenance marker | Refused before any service command |
| `abort` or `restore` start, readiness or service-unit startup fails or is interrupted | Exit 1, or 130 if any step was interrupted or SIGINT or SIGTERM arrived during the cleanup (signals are deferred until every step was attempted); the tool writes the marker back and attempts the shutdown even after a failed or interrupted step, then prints every error with its captured stderr, the observed marker and application containers (an unsuccessful container listing is reported as unknown and needs a successful status check first), and the next step: the same command once none runs and the marker holds the identity, else first the stop command or the identity to write back |
| Interrupted (Ctrl-C) | Exit 130 with the command to run next |
| `reset` without a current session for that object (no `prepare`, an earlier maintenance, another prefix), with no marker, an application container running, a malformed object, a missing record, a missing or changed archive or export, or missing roles | Refused before any purge or drop |
| `restore` of another object once the database changed in this maintenance, of an archive recorded for another object, or of an archive without applied migrations | Refused before any purge or drop |
| `restore` when `migrate --check` fails on the restored database | Exit 1; marker and session kept, release not started |
| A queue keeps messages after the purge | Exit 1 before the drop |
| Downloaded object differs from the bucket's MD5 or size | Refused, partial file removed, nothing dropped |
| Restored row counts differ from the record | Exit 1 with the differing tables; marker kept, release not started; `restore` can be run again |

## Rules and invariants

- Stop order equals the installer's: dispatcher (660 s), worker and notifications (660 s), api and web (100 s). PostgreSQL, RabbitMQ and the gateway with the `maintenance` service stay up, so the gateway keeps answering with the maintenance notice.
- An archive is used only when its record names the confirmed object and its file still has the recorded SHA-256; a record is written only after a throwaway restore succeeded and matched the live row counts, and the Solcast export holds one line per counted snapshot and is uploaded.
- A session ends with its maintenance: the installer does not touch an existing marker's content and removes it on success, and a successful release removes the session, so a later maintenance cannot inherit an earlier one's refusal. A session whose marker identity changed is not current.
- Queues are purged before every drop: primary keys restart at 1, so a leftover message would address an unrelated row.
- The throwaway restore uses the PostgreSQL image of the current release, no network, and is removed in `finally`. The live restore keeps owners and ACLs and applies `bootstrap.sql` again; the role check refuses a runtime role that can write `django_migrations`.
- `abort` and `restore` start the release `/opt/pyracast/current` points to only after its `migrate --check` passes; the tool never changes that link or the installer. A `restore` of a release's own pre-release dump uses the archive and record the release kept on the host, without the bucket.
- The export restore skips a collection whose retry window was still open when the replacement binding was approved and one whose slot the binding already holds, and counts them; coverage only such a collection held is not restored.

## Tests

- `tests/test_deployment_reset.py`: real PostgreSQL verifies schema-qualified table access for readable and unreadable roles. A fake `subprocess` covers stop order and marker identity, verified archives and Solcast exports, refusals, sessions, locking, queue purge, bootstrap and restore order, checksum, host restart, readiness and service-unit failure with retry, errors and interrupts at each step of a failed start with the reported state, and command-line recovery. `tests/test_deployment_helpers.py`: Python 3.12 syntax and `--help` without dependencies.
- `tests/test_operations_contracts.py`: the reset tool stops the application in the installer's drain order; an upgrade records the recovery point right before `migrate`; every printed recovery reads the session. `tests/test_deployment_previous_helper.py` runs each printed recovery through this helper and main's against a model of the database.
- `deploy/smoke.sh` (CI and Release): `prepare` then `abort` serves again, with the maintenance notice answered meanwhile; `prepare`, `reset`, a pre-release dump of the empty database, `migrate` from zero under the restricted owner and a start; an `abort` that is refused after the reset; the stop of a failed release, then `restore`: row counts, table owners, the runtime role's missing DDL right and a stored password are back and the site is ready.
- `deploy/test-release.sh`: the installer with a marker that holds a session succeeds and removes it, or fails in `migrate` and keeps its content; a prepared session becomes replaced, a replaced one stays, and a second release keeps the first run's recovery point.
- `tests/test_provider_snapshot_restore.py`: outcomes, accepted differences and refusals of the export restore.
- Gaps: the real bucket upload and download, the production readiness probe, `systemctl` and the release lock on the VM run first on the host; the proof of a restored sign-in is a stored password checked in the container, not an HTTP login (email verification and MFA). No Playwright coverage applies.
