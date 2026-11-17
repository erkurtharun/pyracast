# Collect EPIAS reported production and settlement injection

The dispatcher's `metering` phase (every poll, default 60 s) turns each enabled EPIAS connection into daily `realtime_generation` jobs and monthly `settlement_injection` jobs, then sends up to 100 due job ids to the `collect_epias` actor. The worker leases one job, fetches EPIAS with the platform-wide credentials, stores one hashed `EpiasSnapshot` per target day and attempt, sets the job outcome and, in the same transaction, asks `prepare_calibration` for a new calibration run. There is no screen or endpoint: results surface in the production-history read, the EPIAS status read and the admin operations page.

## Entry points

- Dispatcher phase `metering` (last of five): `src/pyracast/forecasts/management/commands/dispatch.py` `Command.handle` calls `src/pyracast/metering/runtime.py` `dispatch_epias`, which runs `src/pyracast/metering/scheduler.py` `enqueue_due_epias_jobs`, then `src/pyracast/metering/jobs.py` `pending_epias_ids` (at most 100, oldest `next_attempt_at` first) and `actor.send` for each id.
- Actor `collect_epias` (queue `epias`, `max_retries=0`) from `runtime.make_epias_actor`, consumed by `pyracast worker` (`src/pyracast/forecasts/management/commands/worker.py`, `epias` is in the default queues); `runtime.deliver_epias` calls `jobs.run_epias_job`.
- Initial backfill jobs are also created outside the dispatcher by `scheduler.enqueue_connection(..., initial=True)`: from `src/pyracast/metering/operations.py` `connect_epias` (owned by `epias-connection`) and from `src/pyracast/plants/operations.py` `_save_configuration` whenever a plant with an enabled connection gets a new configuration (`revise_plant`, `activate_submitted_plants`; owned by `plant-configuration-revision`). The next dispatcher cycle sends them.
- External: EPIAS CAS `giris.epias.com.tr/cas/v1/tickets` and transparency `seffaflik.epias.com.tr/electricity-service/v1/generation/data/{realtime-generation, injection-quantity, injection-quantity-powerplant-list}` via `src/pyracast/epias.py` `EpiasClient`, built once per process by `src/pyracast/metering/client.py` `get_epias_client` from `EPIAS_USERNAME` and `EPIAS_PASSWORD`. The plant-list lookup runs in the dispatcher process, the data fetches in the worker.

## Sequence

```mermaid
sequenceDiagram
    participant D as dispatcher
    participant S as scheduler
    participant DB as PostgreSQL
    participant E as EPIAS
    participant Q as RabbitMQ
    participant W as run_epias_job
    D->>S: "enqueue_due_epias_jobs(now)"
    loop each enabled connection whose plant, region are not deleted
        opt no settlement id, last check over 1 day, configuration current, inside nightly window
            S->>E: "CAS ticket, GET injection-quantity-powerplant-list, match EIC (no lock held)"
        end
        S->>DB: "lock Region, Plant, EpiasConnection, store settlement id, check time"
        S->>DB: "get_or_create initial 90 days + 12 months if oldest slot missing"
        S->>DB: "get_or_create 7 daily + 2, 3 or 12 monthly jobs inside 00:00-09:00 window"
    end
    alt scheduler raises
        S-->>D: "exception, nothing sent this cycle"
    else scheduling finished
        D->>DB: "pending_epias_ids: fail jobs past retry_until, select 100 due ids"
        D->>Q: "collect_epias.send(job_id) per id"
    end
    D->>DB: "record_dispatch_phase(metering, error class)"
    Q->>W: "deliver_epias(job_id)"
    W->>DB: "claim: lock Region, Plant, Connection, Job, then check"
    alt not due, leased or terminal
        W-->>Q: "return, nothing written"
    else not current, past retry_until or attempts exhausted
        W->>DB: "superseded or failed"
    else lease granted
        W->>DB: "running, attempts + 1, lease 5 min"
        W->>E: "CAS ticket (cached), paginated POST for day or month"
        alt EpiasError, identity mismatch or unexpected exception
            W->>DB: "_finish_failure: pending with backoff, failed or superseded"
        else response accepted
            W->>DB: "_publish: relock, lease, currency recheck"
            W->>DB: "insert EpiasSnapshot per day, set succeeded, pending, failed or awaiting_publication"
            W->>DB: "prepare_calibration in the same transaction"
            opt preparation raises
                W->>DB: "rollback snapshots, _finish_failure"
            end
        end
    end
```

## Data

| Table | R/W | What |
|---|---|---|
| `metering_epiasconnection` | R/W | Enabled, revision, `provider_plant_id`, EIC; scheduler writes `settlement_plant_id` and `settlement_checked_at`; row lock |
| `metering_epiascollectionjob` | R/W | Created by `get_or_create` on the slot key; lease, `attempts`, `next_attempt_at`, `retry_until`, `status`, `last_error`; bulk expiry in `pending_epias_ids` |
| `metering_epiassnapshot` | R/W | One per job, attempt and target day: filtered raw rows, normalized readings, `content_sha256`, `complete`, `available_at`; `prepare_calibration` reads them back in the same transaction |
| `regions_region` | R | Row lock (first in order), `timezone`, `deleted_at`, approval state through `binding_is_current` |
| `plants_plant`, `plants_plantconfiguration` | R | Row lock, `current_configuration`, `deleted_at`; `configuration_is_current` |
| `connections_sitebinding`, `connections_provideraccount` | R | `binding_is_current` inside `configuration_is_current` |
| `forecasts_collectionschedule` | R | `enabled` and `effective_from` gate the nightly window; a disabled schedule supersedes windowed jobs |
| `plants_calibrationrun`, `plants_calibrationactivation`, `plants_plantevent` | R/W, R, W | `prepare_calibration` skips while a run is `pending` or `running`, compares earlier runs of the latest activation, then may insert a pending run and a `calibration_scheduled` event (also reads observations) |
| `forecasts_dispatcherstatus` | W | `record_dispatch_phase("metering", ...)` after every cycle |

## Authorization

- No user principal. Tenant scope follows job -> connection -> plant -> region; every write is keyed by that job or connection. Which EPIAS plant feeds a plant was fixed by the platform administrator in `connect_epias`; each job pins that identity in `provider_plant`.
- The scheduler only takes connections with `enabled=True` on non-deleted plants and regions, and `enqueue_connection` requires a current configuration (`plants/operations.py` `configuration_is_current`).
- `jobs._current` runs at claim, after failure and at publication under the row locks: connection enabled, revision equal, `provider_plant` id and EIC equal to the connection (settlement id for settlement jobs), configuration still current, and for windowed jobs no disabled schedule.
- Lease ownership (`_owns`: running, same token, unexpired) is rechecked under lock before any failure or publication write.
- One platform credential pair is shared by all tenants. It is sent only in the login form to the fixed CAS host; the ticket goes only in the `TGT` header to the fixed data host. `EpiasClient` follows no redirects and rejects responses echoing the username, password or ticket (`unsafe_epias_response`).
- Database: unique slot (`epias_unique_collection_slot`), unique acquisition (`epias_unique_job_acquisition`), status and source check constraints, deferred composite FKs pinning snapshot -> job -> connection, configuration and plant (migrations `0005`, `0009`).

## Failure paths

| Condition | Result |
|---|---|
| Any exception in `enqueue_due_epias_jobs` (one connection is enough) | Phase fails, no id is sent this cycle; `DispatcherStatus.last_error` = exception class, admin "dispatcher phases" check; a one-shot `dispatch` exits with `CommandError` |
| `actor.send` raises (RabbitMQ unavailable) | Remaining ids are not sent and the phase fails the same way; jobs stay `pending` and are resent next cycle |
| Settlement lookup fails with `EpiasError`, finds nothing or is ambiguous | `settlement_plant_id` stays null, `settlement_checked_at` = now, retried after 1 day inside the window |
| Job or a parent row missing, job not due, leased or terminal | `claim_epias_job` returns None, nothing written |
| Connection disabled or changed, configuration replaced, plant or region deleted, region binding not current, schedule disabled (windowed jobs) | `superseded`, terminal |
| Past `retry_until` at claim, before the request or in `pending_epias_ids` | `failed`, `retry_window_expired` |
| Job claimed again after three attempts, for example after its third lease expired (yesterday's nightly job is exempt) | `failed`, `attempts_exhausted` |
| Missing credentials | `EpiasError("credentials_unavailable")`, retryable, `epias_provider_failure` |
| HTTP 408, 425, 429, 5xx except 501/505, 401 from a data endpoint, transport error, schema, interval or unsafe-response errors | `pending` with backoff (`Retry-After` honored on 429/503), `epias_provider_failure`; `failed` when the budget or window ends |
| Any other unexpected HTTP status (for example 400, 403, 404, a redirect) or a 401 from CAS | `failed`, `epias_provider_failure` |
| Response plant, day, source kind or period differs from the job | `epias_response_identity_mismatch` raised, recorded as `epias_provider_failure`, retried like a provider failure |
| Lease lost before a failure or publication write | Response discarded, nothing written; the job stays `running` until it is due again and `pending_epias_ids` resends it (`interrupted_metering` in admin health) |
| Realtime day incomplete (not 24 hourly labels with totals) | Snapshot kept, `partial_data`; yesterday's nightly job retries every 30 min to 09:00, older windowed days every 30 min, backfill days after 30 then 60 min; `failed` after three attempts |
| Settlement period empty or incomplete | Snapshots kept, job ends `awaiting_publication` with `unpublished_data` or `partial_data`; only a later nightly job refetches, and only for the last 2 (Monday 3) months |
| `prepare_calibration` raises | Snapshots rolled back, attempt counted, `epias_provider_failure`, retried with backoff |

## Rules and invariants

- Row-lock order: Region, Plant, EpiasConnection, EpiasCollectionJob; `prepare_calibration` relocks Region and Plant in the same transaction. No lock is held during EPIAS calls (settlement lookup runs before the scheduler transaction). The bulk expiry in `pending_epias_ids` is a plain `UPDATE` without parent locks or a currency check.
- Nightly window: region-local 00:00 (first existing half hour) to 09:00 (`collection_window.nightly_window`); no daily jobs if the connection changed inside tonight's window or the schedule is disabled or not yet effective. Target days are counted in Europe/Istanbul.
- Daily run: yesterday plus six older days and, once a settlement id is known, 2 settlement months (3 on Monday, 12 if the revision has no other settlement jobs); every job gets `retry_until` = 09:00. Only yesterday's realtime job retries without an attempt cap; claiming a windowed job pushes `next_attempt_at` 30 min forward.
- Initial backfill: 90 realtime days and 12 completed months keyed on `scheduled_for = max(connection.updated_at, configuration.created_at)`, so repeated scheduling is idempotent; it is re-run whenever the oldest day or month slot is missing for the current revision and configuration. Only the previous-day job inherits a deadline when backfill starts inside the window.
- `succeeded`, `failed`, `superseded` and `awaiting_publication` are terminal. Slots are looked up by key whatever their status, so a terminal slot is never retried; new work needs a new `scheduled_for`, connection revision or configuration.
- `retry_until` is checked at claim, before the request and when a retry is scheduled, not at publication: a response requested before 09:00 is still stored after it.
- Settlement requests cover one calendar month (at most 31 days) with page size 100; realtime requests cover one day. The CAS ticket is reused for 7,140 s per process and dropped on any 401 or 403.
- Snapshots are never updated; a retry writes a new row per attempt. Missing hours stay missing, not zero; readings use `hour-start-istanbul-v1`.
- Leases last 5 minutes; every step adds monotonic elapsed time to `now`. Dramatiq never retries and keeps its default 10-minute time limit; the dispatcher resends due ids every cycle and the claim discards duplicates.
- EPIAS jobs use no Solcast quota and have no request budget of their own, but scheduling and `_current` need the region's provider binding to be current (`forecasts/jobs.py` `binding_is_current`).
- `delivery_health` counts `overdue_metering`, `interrupted_metering` and `failed_metering` (failed jobs scheduled in the last day); `awaiting_publication` is not counted.

## Tests

- `tests/test_metering.py`: scope, duplicate workers, lease expiry, failure redaction, identity mismatch, supersession, partial data, purge during claim or scheduling. `tests/test_metering_nightly.py`: window, deadlines, 30-minute retries, schedule disable.
- `tests/test_metering_settlement.py`, `tests/test_metering_history_backfill.py`: monthly split, unpublished settlement, EIC lookup outside locks, 90-day and 12-month backfill idempotency, backfill after a configuration revision, rollback when preparation fails.
- `tests/test_epias.py`, `tests/test_epias_settlement.py`, `tests/test_collection_window.py`: client parsing, pagination, TGT reuse, redaction, window edges. `tests/test_metering_runtime.py`: credentials cache, duplicate dispatch through the stub broker, opt-in RabbitMQ delivery (`PYRACAST_LIVE_QUEUE=1`). `tests/test_epias_live.py`: captured-fixture replay and opt-in live check (`PYRACAST_EPIAS_LIVE=1`).
- Phase level (with `dispatch_epias` mocked): `tests/test_dispatch_resilience.py`. Health counters: `tests/test_delivery_health.py`, `tests/test_admin_health_queries.py`, `tests/test_admin_health_views.py`.
- Gaps: no test pins what `dispatch_epias` does when the scheduler or `actor.send` raises, nor one failing connection blocking the rest; the America/New_York test (`test_metering.py` `test_daily_slot_uses_region_night_independently_of_forecast_times`) pins window, `scheduled_for` and target days only at a moment where region and Istanbul dates agree, so none separates Istanbul from region-local target days; none for a lease expiring during a multi-page fetch, or recovery after a binding was briefly not current. No Playwright coverage (no UI); `web/tests/epias-production.spec.ts` covers the production screen with mocked APIs.
