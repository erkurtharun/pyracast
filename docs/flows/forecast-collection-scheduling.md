# Forecast collection scheduling and publishing of due jobs

The `pyracast dispatch` loop (every 30 s in production) runs the `collection` phase. It creates slot-unique `CollectionJob` rows per approved region: inside the local night window the daily history job, then, when a forecast clock is due, the job for the latest due clock and its intraday estimated-actuals pair. It then sends up to 100 due job ids, oldest `available_at` first, to the `collect_forecast` Dramatiq actor. Execution (claim, fetch, publish) is the `forecast-collection-execution` flow.

## Entry points

- Command: `src/pyracast/orchestration/management/commands/dispatch.py` `Command.handle` -> `orchestration/dispatcher.py` `run`, whose `phases` run `notifications`, `collection`, `calibration`, `retention`, `metering` in this order, once or with `--loop --poll-seconds` (default 30). Between cycles it publishes outbound mail every 2 s. Production: `deploy/compose.yaml` (`--poll-seconds 30`, heartbeat file).
- Phase: `src/pyracast/forecasts/runtime.py` `dispatch_collections(actor)` (`now` is taken once, so jobs created this cycle are due in the same cycle, but are sent only if they fit the 100-id cap). Scheduling: `forecasts/scheduler.py` `enqueue_due(now)` -> `_enqueue_region` (`collection_gate`, `_schedule_night_history`, `_schedule_forecast_slot`, `_schedule_paired_actuals`), using `forecasts/timing.py` (`forecast_timing`, `latest_forecast_slot`) and `collection_window.py` `nightly_window`. Publishing: `forecasts/jobs/ready.py` `pending_job_ids(now, limit=100)`.
- Inputs created elsewhere: `plants/approval.py` `approve_region` (the first job), `forecasts/schedule.py` `configure_schedule` (flow `collection-schedule`), and follow-up history jobs from `forecasts/jobs/publish.py` `publish` (execution flow). No screen, endpoint or user actor.

## Sequence

```mermaid
sequenceDiagram
    participant C as dispatch command
    participant R as dispatch_collections
    participant S as enqueue_due
    participant D as PostgreSQL
    participant Q as RabbitMQ
    C->>R: "collection phase"
    R->>S: "enqueue_due(now)"
    loop "each approved, not trashed region"
        S->>D: "BEGIN, lock region row, read binding and schedule"
        alt "collection_gate refuses: binding not validated or not current, schedule disabled, no first job"
            S->>D: "COMMIT, nothing created"
        else "collecting"
            opt "now inside local 00:00 to 09:00"
                S->>D: "get or create history job, revive stale ones"
            end
            opt "a clock slot is due since effective_from"
                S->>D: "mark older pending and retry forecast jobs stale"
                S->>D: "get or create latest slot job, revive if stale"
                opt "now within 15 min of slot"
                    S->>D: "get or create paired estimated_actuals job (a 00:00 slot reuses the history job)"
                end
            end
            S->>D: "COMMIT"
        end
        Note over S: "a region that raises is logged with its origin and remembered, the loop continues"
    end
    S-->>R: "ids (discarded), or CollectionSchedulingError with the failed region ids after the loop"
    R->>Q: "queue depth, skip publishing at 1000 or more"
    R->>D: "pending_job_ids(now), max 100, oldest first"
    R->>Q: "actor.send(job_id) for each id"
    R-->>C: "PhaseOutcome: published count and failure code"
    C->>D: "record DispatcherStatus with duration and published count (a failed write is only logged)"
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `regions_region` | R | Approved, not trashed regions; `FOR UPDATE` row lock per region; `approved_binding`, `revision`, `timezone` |
| `connections_sitebinding` | R | `validated_at`, `region_revision`, `validated_credential_revision` |
| `connections_provideraccount` | R | `enabled`, `authorized_at`, `credential_revision`, `daily_quota` (trims clocks), `request_blocked_until` (publishing) |
| `forecasts_collectionschedule` | R | `enabled`, `times`, `effective_from`; no row means one clock at `PYRACAST_CUTOFF_LOCAL` |
| `forecasts_collectionjob` | R/W | Slot jobs via `get_or_create`; `status` moves to `stale` or back to `pending` |
| `forecasts_forecastsnapshot` | R | Only to skip reviving history jobs that already have a snapshot |
| `forecasts_dispatcherstatus` | R/W | Per-phase attempt, success, error code, duration and published count, upserted by the dispatcher (`update_or_create`); no `RequestAttempt` here, quota is reserved at execution |

## Authorization

None. A system process with direct database access and no tenant context. Scope is "approved and not trashed" plus `scheduler.collection_gate`: `validated_at` set, `binding_is_current` (region approved, same revision and binding; account enabled and authorized; credential revision validated), schedule not disabled and a first forecasts job present. `claim` rechecks `binding_is_current` and the schedule at execution.

## Failure paths

| Condition | Result |
| --- | --- |
| Naive `now` | `ValidationError` for direct callers of `enqueue_due`; `dispatch_collections` always passes an aware `now` |
| One region raises (lock wait over 5 s, DB error) | That region rolls back and `collection_region_scheduling_failed region_id=<id> error_type=<class> origin=<location>` is logged; others continue; after the loop `CollectionSchedulingError` carries the failed region ids |
| `enqueue_due` raised | `dispatch_collections` closes unusable connections, still sends pending ids and returns the published count with `collection_regions_failed:<count>` (or the class name of any other error, logged as `collection_scheduling_failed`) |
| Queue holds 1000 or more messages | `publication_skipped` is logged and nothing is sent; the ids stay due in the database |
| Broker or `pending_job_ids` fails | `collection_publish_failed` is logged with the count sent so far; the outcome keeps an earlier scheduling code, otherwise records the class name; ids already sent stay sent, the rest stay due |
| Phase failed | The dispatcher logs `dispatch_phase_failed` for a raising phase, stores the failure code in `DispatcherStatus.last_error` (unhealthy in `delivery_health`), runs the remaining phases; a phase whose items failed stores `items_failed:<n>`; one-shot mode ends with `CommandError`. If recording the status itself fails, the row stays unchanged, `dispatch_monitoring_failed` is logged, later phases still run and one-shot mode also ends with `CommandError` |
| No first job, stale binding, disabled schedule | Region skipped silently; leftover jobs are still published and closed by `claim` |

## Rules and invariants

- Idempotency: `collectionjob_binding_endpoint_scheduled_for_uniq` (binding, endpoint, `scheduled_for`; forecasts and estimated-actuals jobs only, imports and scaled copies take their slot from their source) plus the region row lock. Repeated or concurrent dispatchers create no duplicates.
- Only the latest slot of yesterday and today is scheduled, so a long outage collapses to one job. A newer slot marks older `pending` and `retry` forecast jobs `stale` (`forecast_superseded`); `running` jobs are untouched. A stale job of the latest slot is opened again (`pending`, `last_error` cleared) unless its `last_error` is `quota_exhausted` (the binding's day bound) or `schedule_disabled`; those end the slot.
- `first_slot` is the slot of the binding's first created forecasts job, the off-clock approval job (`timing.approval_job`). `effective_from` is the later of it and `schedule.effective_from`, so neither approval nor edits backfill earlier slots. `forecast_timing` is the one timing decision shared with `claim`, the quota ledger, the schedule screen and reminder preparation.
- Forecast clocks are trimmed to `daily_quota - 1`; one request per day is reserved for history. With `daily_quota` 1 only history is scheduled.
- Night window: local 00:00 (first valid half-hour if DST skips it) to 09:00, collecting the day before. One history job per local night with `retry_until` at the window end; an existing job with that deadline is reused. A stale history job of the window that saved no snapshot and can still be claimed before the window ends is revived (`pending`, `available_at` kept or moved to now, `last_error` cleared), unless a planned skip ended it (`NIGHT_FINAL_ERRORS`: provider day or the binding's day bound). A disabled schedule stops new night work.
- The paired actuals job exists only if scheduling runs within 15 minutes of the slot. A 00:00 clock slot equals the night window start, so the pair's `get_or_create` (same binding, endpoint and slot) returns the history job created just before: no separate intraday job, target date stays yesterday and the deadline stays 09:00. Revival keeps `attempts`.
- Publishing is at-least-once and capped: each cycle resends the first 100 due ids ordered by (`available_at`, pk) without dedup, so a backlog drains over later cycles; `claim` (lease plus region lock) makes duplicates harmless. The actor has `max_retries=0`; retry policy lives in the database. `pending_job_ids` skips trashed regions, wakes throttled `retry` jobs once the account cooldown is cleared and republishes `running` jobs whose lease expired and whose `available_at` has passed.

## Tests

- `tests/test_jobs.py`: dedup and missed-slot recovery, unapproved or unvalidated binding, naive `now`, concurrent approvals.
- `tests/test_collection_dispatch_isolation.py`: pending ids sent after scheduler failure, contended region isolated. `tests/test_worker_runtime.py`: no publication into a queue 1000 deep. `tests/test_runtime.py`: schedule before send, repeated sends, command modes. `tests/test_dispatch_resilience.py`, `tests/test_dispatch_recovery.py`: command phases, failure codes and reconnect.
- `tests/test_collection_failures.py` (hundred oldest due jobs, revived slot job without its old outcome), `tests/test_collection_timing.py` (reopened latest slot, daily bound), `tests/test_revalidation_schedule.py` (approval slot), `tests/test_collection_resilience.py` (slot collapse, supersession), `tests/test_cutoff.py` (DST), `tests/test_collection_window.py`, `tests/test_nightly_solcast.py` (night window, revival), `tests/test_collection_schedule.py` (paired jobs, concurrent daytime schedulers, history revival keeps attempts), `tests/test_collection_calendar_windows.py` (durable same-slot pair).
- Gaps: no test of a forecast clock of 00:00 sharing the history slot. No Playwright test; the flow has no UI.
