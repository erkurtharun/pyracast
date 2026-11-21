# Purge trashed sites after 30 days

Every dispatcher cycle runs the `retention` phase, whose first step hard-deletes regions whose `deleted_at` is at least 30 days old, together with their plant and everything they own. Nobody calls it from the web or the API. The outcome is that the rows vanish from the recycle bin and can no longer be restored, while provider accounts and their request-attempt quota ledger survive.

## Entry points

- Command: `pyracast dispatch --loop` (`pyracast.orchestration.management.commands.dispatch` `Command.handle`) runs `pyracast.orchestration.dispatcher` `run`; its `_retention` calls `pyracast.trash.purge_expired` first, before the purge warning (`pyracast.purge_warning`) and the account and mail sweeps. Retention is the fourth of five phases (notifications, collection, calibration, retention, metering). The loop polls every 30 s. Without `--loop` one cycle runs and a failed phase raises `CommandError` after the other phases.
- Internals: `purge_expired` calls `_purge_expired_region` per region, which calls `_purge_region`, which calls `_purge_plant` for the region's plant. A naive `now` raises `ValidationError`.
- Screen: none triggers it. `web/src/features/trash/recycle-bin.tsx` `RecycleBin` shows `purge_after` and disables restore when `restore_blocker` is set (`pyracast.api.trash` `TrashedRegionOutput`); a purged row vanishes on the next list load.
- Actor: the dispatcher process with the application database role. No user, tenant or queue message is involved.

## Sequence

```mermaid
sequenceDiagram
    participant C as dispatch loop
    participant P as purge_expired
    participant D as PostgreSQL
    participant M as DispatcherStatus
    C->>P: "retention phase, every cycle"
    P->>D: "regions with deleted_at <= now-30d or purge begun, begun first then lowest 100 ids"
    loop each region id while the statement budget lasts, own transaction
        P->>D: "lock region, recheck it is still purgeable"
        alt restored or already purged
            P->>P: skip
        else still expired
            P->>D: "first time: set purge_started_at"
            P->>D: "lock the plant, then provider accounts"
            P->>D: "delete in chunks: plant history, deliveries and schedules; null request-attempt links"
            P->>D: "delete archive, jobs, imports, bindings, audit rows, region"
            alt budget spent
                P->>P: "commit the prefix reached, resume next cycle"
            else region gone
                P->>P: "after commit: log trash_purged"
            end
        end
    end
    alt a region raised
        P-->>C: "ItemsFailedError after the last region, failed ones skipped 15 minutes"
        C->>M: "last_error = items_failed:N"
    else ok
        P-->>C: "counts dict, discarded"
        C->>M: "attempted_at and succeeded_at"
    end
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `regions_region`, `plants_plant` | R/W | Regions by `deleted_at` or `purge_started_at`, locked `FOR UPDATE` with their plant; `purge_started_at` set; deleted last |
| `connections_provideraccount` | R | Locked `FOR UPDATE` (pk order) for the region's bindings; never deleted |
| `forecasts_requestattempt` | W | `job` and `binding` set to NULL; rows kept so quota counters do not reset |
| `forecasts_collectionjob`, `forecasts_forecastsnapshot`, `forecasts_forecastresult`, `forecasts_referencecapacitynormalization`, `forecasts_historicalreferenceimport` | W | Region archive, deleted in the order normalization, result, snapshot, job, import |
| `forecasts_collectionschedule`, `notifications_reminderschedule`, `notifications_reminderrecipient`, `notifications_reminderrecipienttie`, `notifications_notificationdelivery`, `notifications_deliveryrecipient` | W | Region schedules, their recipients and all deliveries of the region, recipient rows first |
| `connections_sitebinding`, `regions_auditevent`, `regions_regionlocationchangerequest` | W | Bindings (after `approved_binding` is nulled and the status reset to `pending`), region audit rows, location requests |
| `plants_plantconfiguration`, `plants_forecastissuance`, `plants_productionobservation`, `plants_plantevent`, `plants_plantsubmission`, `plants_calibrationrun`, `plants_calibrationactivation` | W | Plant history; `current_configuration` and `comparison_activation` nulled first; observations deleted leaves first along `supersedes` |
| `metering_epias*` (snapshot, job, connection revision, connection), `change_requests_changerequest` with its items and incident changes, `incidents_plantincident`, `incidents_incidentrevision` | W | EPIAS in that order; change-request children first |
| `forecasts_dispatcherstatus` | W | `retention` row: attempt, success, error code (`record_dispatch_phase`) |

## Authorization

- No per-user check: the phase selects rows only by `deleted_at` and `purge_started_at`, across all tenants. Authorization happened when a workspace administrator trashed the item (`plant-trash-and-restore`).
- Database: rows are deleted explicitly, children before parents, in primary-key chunks of 500 (`PURGE_CHUNK_SIZE`) by raw statements that read no column but the key, so Django's cascades never run; a child the purge forgets leaves a reference the deferred foreign keys reject when the transaction commits. `tests/test_purge_graph.py` lists every foreign key among purged rows with its handling. No trigger blocks deleting archives: the issuance guard (`immutable_forecast_issuance`, declared in `pyracast/schema_objects.py`) fires on INSERT and UPDATE only.

## Failure paths

| Condition | Result |
| --- | --- |
| Candidate restored or purged elsewhere between listing and lock | Skipped silently: purgeability is rechecked under the lock |
| Exception in a region (foreign-key violation, 5 s lock timeout, 60 s statement timeout, lost connection) | That region rolls back (including `purge_started_at`), is logged as `retention_purge_failed item=region:<id>` and skipped for 15 minutes in this dispatcher process; later regions and the other retention steps still run; the phase ends `items_failed:N` |
| Exception outside a region (selection, a step as a whole) | The step raises and the phase ends there; log `dispatch_phase_failed phase=retention`, `DispatcherStatus.last_error` = class name |
| Failed phase without `--loop` | Other phases still run, then `CommandError` |
| Stale or failed phase | `delivery_health` lists `retention` in `unhealthy_dispatch_phases`; the admin console shows it and `check_delivery_health` exits with `CommandError` |
| Restore after purge, or between deadline and purge | 404 `not_found`, or 400 `trash_retention_expired` ("The 30-day restore period has ended."); a purge that has begun also refuses it |
| Queued message for a purged row | `claim`, `collect`, `run_calibration` and `process_notification` return `None`/`False`; the collection `deliver` returns without work |

## Rules and invariants

- The boundary is inclusive and gapless: purge selects `deleted_at <= now - 30 days`, restore refuses when `now >= deleted_at + 30 days` (`RETENTION`, `restore_blocker`), and the API's `purge_after` is the same instant. Re-trashing keeps the first `deleted_at`; restore clears it, so a later trash starts a new 30 days. The first purge transaction sets `purge_started_at`: restore then refuses the region whatever the clock says, the bin leaves it out and `deleted_at` is never rewritten.
- Work per call is bounded: at most 100 regions (`PURGE_BATCH_SIZE`) and 200 chunk statements (`PURGE_STATEMENT_BUDGET`) across them. A region that exhausts the budget commits the consistent prefix it reached and resumes first on the next cycle, before regions not yet started, lowest pk first. A backlog drains over several cycles.
- Lock order is region, plant, provider accounts by pk, the same relative order as the collection commit (region, job, plant, account). Locking the region first serializes against restore, trash and worker commits; plant rows are deleted only by `_purge_plant` under that lock. The tenant row is not locked. Cyclic links are cleared first (`approved_binding`, `current_configuration`, `comparison_activation`, request-attempt links).
- Quota is never refunded: the account lock keeps reservations from interleaving, and attempt rows lose only their links. Purging a region removes its audit events, so `region_trashed` history ends with it.
- A completed purge logs one INFO record `trash_purged` after commit (region, tenant, deletion time, plant and binding ids; no name, place or key). A purge that runs out of budget logs nothing until the region is gone. A failed status write is logged as `dispatch_monitoring_failed`.

## Tests

- `tests/test_trash_operations.py`: boundary (microsecond before, exact deadline), region and plant graphs, quota ledger and sibling regions kept, batch size, naive clock, concurrent purge, bounded statements, small budgets. `tests/test_purge_graph.py`: every foreign key among purged rows has a rule.
- `tests/test_trash_boundaries.py`: purge racing collection, reminder and calibration candidates before the region lock; purged queue ids ignored. `tests/test_trash_restore_rules.py`: the `trash_purged` record, plant rows deleted only under the region lock. `tests/test_trash_api.py`: expired rows stay listed with a `restore_blocker`, a partly purged region leaves the bin. Child graphs and races also in `test_forecast_archive_core.py`, `test_timeseries_integrity.py`, `test_incidents.py`, `test_change_requests.py`, `test_region_editing.py`, `test_notifications_races.py`, `test_metering.py`.
- `tests/test_dispatch_isolation.py` and `tests/test_account_retention.py`: a failing region does not stop later regions or steps and is deferred. `test_delivery_health.py` seeds a failed `retention` row by hand. Playwright `web/tests/workspace.spec.ts` "recycle bin disables expired records" mocks the API.
- Gaps: volume and statement timeouts, new foreign keys to `Region` or `Plant` beyond the graph test, and the command purging real rows are untested.
