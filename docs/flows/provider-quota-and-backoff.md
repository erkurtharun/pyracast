# Provider quota ledger, forecast budget, cooldown and credential blocks

Every Solcast or fixture request made by the collection worker, by connection validation in the console or by the `check_solcast.py --live` script first reserves one `RequestAttempt` under a row lock on the shared `ProviderAccount`, against that account's UTC-day quota. A forecast job must also fit the binding's own day bound, and `estimated_actuals` jobs (night history and the 15-minute job beside each forecast slot) must leave the requests still needed by scheduled forecasts. A provider 401 blocks the account's current credential revision, a 402 or 429 sets an account-wide wait; collection jobs then wait, retry, end or fail, and a platform administrator recovers through key rotation plus validation or an audited cooldown reset. Accounts are shared across tenants, so one account's state affects every site bound to it.

## Entry points

- Worker: Dramatiq actor `collect_forecast` (`orchestration.kinds.COLLECTION`, deliver in `src/pyracast/forecasts/runtime.py`, `max_retries=0`) -> `forecasts/jobs/collect.py` `collect` -> `jobs/claim.py` `claim` and `retry`, and `forecasts/quota.py` `reserve_request` and `record_provider_failure`. The three-attempt limit is `background.retry_policy.PROVIDER_JOB`.
- Backoff helpers in `src/pyracast/provider_retry.py`: `rate_limit_retry_after` (Retry-After or 900 s for a 429), `next_retry_at` (attempt backoff, minimum, Retry-After, 0-30 s jitter, capped at `MAX_RETRY_AT`) and `next_provider_day` (UTC midnight plus two minutes), called by `record_provider_failure` for the account wait and by `retry` in `jobs/claim.py` for `available_at`; `src/pyracast/metering/jobs.py` also uses `next_retry_at`.
- Dispatcher: `pyracast dispatch` -> `forecasts/runtime.py` `dispatch_collections` -> `jobs/ready.py` `pending_job_ids`, which also returns `provider_throttled` retries whose account has `request_blocked_until IS NULL`.
- Console: `GET|POST /admin/plants/<region_id>/` (`pyracast.platform_admin.plant_review.plant`); `action=validate` -> `validate` -> `forecasts/validation.py` `validate_binding`, `action=clear_cooldown` -> `clear_cooldown` -> `src/pyracast/connections/recovery.py` `clear_provider_cooldown`. Template `admin/regions/region/setup.html` shows the "Provider request delay" callout only while the wait lies in the future. Account creation and key rotation are the console setup action (`connections.setup.save_provider_setup`, `connections.operations.create_account`).
- Script: `scripts/check_solcast.py --live --binding-id N --kind settings|forecast` -> `capture` -> `reserve_account_request` (purpose `inspect_settings` or `inspect_forecast`), `record_provider_failure`.
- Development: `manage.py fixture_demo` (refuses unless `DEBUG`) calls `validate_binding` and `collect` directly on a fixture account.
- Read-only surfaces: the plant's own requests of the UTC day in `GET /api/tenants/<t>/regions/<r>/collection-schedule/` (`schedule_payload`, flow `collection-schedule`); account-level usage, waits and blocks only in the console (`orchestration/delivery_health.py` `check` rendered by `platform_admin/health.py`, and the read-only request attempt records).

## Sequence

```mermaid
sequenceDiagram
    participant W as Worker collect
    participant A as Admin or script
    participant R as reserve_account_request
    participant DB as PostgreSQL
    participant S as Provider
    W->>R: "reserve_request(binding, purpose forecast or estimated_actuals, job)"
    A->>R: "reserve, purpose validation or inspect_settings or inspect_forecast, no job"
    R->>DB: "SELECT ProviderAccount FOR UPDATE, count RequestAttempt of the UTC day"
    alt job past retry_until
        R-->>W: HistoryWindowClosedError
    else authentication_failed_revision equals credential_revision
        R-->>W: "RequestDeferredError(None)"
    else request_blocked_until in the future
        R-->>W: "RequestDeferredError(until)"
    else disabled or unauthorized
        R-->>W: AccountUnavailableError
    else used has reached daily_quota
        R-->>W: QuotaExceededError
    else forecast job beyond the binding's day bound or collection disabled
        R-->>W: "BindingDayExhaustedError or ScheduleDisabledError"
    else estimated_actuals job and used plus one plus remaining forecasts exceeds daily_quota
        R-->>W: ForecastBudgetReservedError
    else admitted
        R->>DB: "INSERT RequestAttempt outcome=uncertain"
        R-->>W: attempt
        W->>S: "HTTP GET after the reservation committed"
        A->>S: "adapter.validate or script GET, also after commit"
        alt success
            W->>DB: "attempt outcome=succeeded, persist snapshot"
        else ProviderError
            W->>DB: "attempt outcome=failed with http_status"
            opt status 401, 402 or 429 and credential_revision unchanged
                W->>DB: "set authentication_failed_revision or extend request_blocked_until"
            end
            W->>DB: "retry(): CollectionJob status retry, stale or failed"
        else any other exception
            W->>DB: "attempt stays uncertain, retry() with provider_unexpected_failure"
        end
    end
    Note over W,A: "Refusals return to whichever caller reserved. On a refusal the worker only calls retry() and returns, with no provider call"
    A->>DB: "clear_provider_cooldown: request_blocked_until=NULL plus AuditEvent"
    A->>DB: "validate_binding success: binding healthy, re-arm latest auth-failed forecast job"
```

## Data

| Table | R/W | What |
|---|---|---|
| `forecasts_requestattempt` | R/W | The ledger: one row per admitted request (`utc_day`, `purpose`, `outcome` uncertain/succeeded/failed, the provider's `http_status`, `correlation_id`); counted per account and UTC day. `purpose` is `RequestPurpose` (`requestattempt_purpose_chk`). Purge keeps rows and nulls `binding`/`job`. |
| `connections_provideraccount` | R/W | Row lock for every reservation; reads `daily_quota`, `enabled`, `authorized_at`, `credential_revision`; writes `request_blocked_until`, `authentication_failed_revision`. The database refuses a quota outside 1..10 (`provideraccount_daily_quota_chk`) and a failed revision the account never had. |
| `connections_sitebinding` | R/W | Bindings of the account for the forecast budget; validation writes `health`, `validated_at`, `validated_credential_revision`. |
| `forecasts_collectionjob` | R/W | The budget reads the open jobs of each binding and its forecast jobs of the UTC day; `claim` and `retry` write `status`, `available_at`, `attempts`, `last_error` and the lease; validation re-arms an auth-failed job. |
| `forecasts_collectionschedule` | R | Enabled flag, local clocks and `effective_from` through `forecast_timing`: the day bound and future forecast slots in the budget. |
| `regions_region` | R | Approved, not deleted, `approved_binding` for budget eligibility; row-locked by `claim` and by validation. |
| `regions_auditevent` | W | `provider_cooldown_cleared`, `connection_validated`, `connection_validation_failed`. |

## Authorization

- Worker path has no user: admission relies on `claim` -> `binding_is_current` (approved region at the binding's revision, enabled and authorized account, binding validated at the current credential revision; `connections/currency.py`), and `reserve_account_request` rechecks `enabled` and `authorized_at` under the lock.
- Console: `PlatformAdminSite.has_permission` (active staff superuser plus verified MFA); every unsafe request also needs a recent confirmation with the authenticator (`admin_view`). `validate_binding` and `clear_provider_cooldown` call `require_platform_admin`. `BindingActionForm.current_binding` rejects stale region or credential revisions; the validate form also names the validation it saw, so a repeated submit spends no second request. The "provider recovered" checkbox is enforced only by the form.
- Script: shell, database and encryption-key access only, gated by the explicit `--live` flag; no application identity and no audit event. `fixture_demo` needs `DEBUG` and an active staff superuser name.
- Tenants never write this state; members read only the plant's own request count through the schedule read (`require_member` by route policy).

## Failure paths

| Condition | Result |
|---|---|
| Worker: `RequestDeferredError(None)` (credential blocked) | Job `failed`, `provider_authentication_failed`; attempt returned |
| Worker: `RequestDeferredError(until)` | Job `retry`, `provider_throttled`, `available_at` = now + max(backoff, time to the wait's end) + 0-30 s jitter; attempt returned; a windowed job is `failed` when that reaches `retry_until` |
| Worker: account disabled or unauthorized, or quota used | `account_unavailable` or `quota_exhausted`, attempt returned; the job waits for the next provider day (UTC midnight plus two minutes), a windowed job is `stale` when that reaches `retry_until` |
| Worker: forecast job beyond the binding's day bound, or collection disabled after the claim | Job `stale`, `quota_exhausted` or `schedule_disabled`; attempt returned |
| Worker: `ForecastBudgetReservedError` (`estimated_actuals` job) | `history_budget_reserved`, attempt returned, next try no sooner than 30 min; `stale` when that reaches `retry_until`, which is always the case for the 15-minute job |
| `claim` or reservation past `retry_until`, or window closed after reservation | `failed`, `history_window_closed` (the post-reservation case marks the attempt `failed`) |
| Provider 401 | Account blocked for this credential revision; job `failed`, `provider_authentication_failed` |
| Provider 402 | Account blocked until the next provider day; job `retry`, `provider_allowance_exhausted` at that moment, attempt returned; a windowed job is `stale` when that reaches `retry_until` |
| Provider 429 | Account wait = max(existing, now + max(60 s, Retry-After or 900 s) + 0-30 s jitter); job `retry`, `provider_throttled`; the execution attempt is kept, so the third one fails a forecast job |
| Provider 403 or 404, other 3xx/4xx, 501, 505, or an invalid, oversized or key-echoing answer | Attempt `failed`; job `failed`, `provider_access_denied`, `provider_site_not_found`, `provider_failure`, `provider_server_error` or `provider_invalid_response`; no account state change, no second request |
| Provider 408, 425, other 5xx, transport error, non-JSON body, or a body not complete within 30 s | Attempt `failed`; job `retry`, `provider_failure`, `provider_server_error`, `provider_unreachable` or `provider_invalid_response`; a forecast job waits at least 4 then 8 min and is `failed` after 3 attempts, a history job retries inside its window |
| Any other exception during the provider call | Attempt stays `uncertain` and still counts; job `retry`, `provider_unexpected_failure`; forecast job `failed` after 3 attempts |
| Key rotated, binding not yet validated at the new revision | `claim` marks the dispatched job `stale` (`binding_not_current`) without a provider request |
| Validate: Solcast binding without settings reference or installation date | `ValidationError` before any reservation; the console shows its generic setup error |
| Validate: region revision differs from the binding | `ConflictError` "no longer current" before any reservation |
| Validate: an unanswered validation attempt younger than a minute exists | `ConflictError`, no request |
| Validate: region or key changed during the provider call | `ValidationError`; the attempt stays `succeeded` and the binding unchanged |
| Validate: provider error | Attempt `failed` with its status, binding `health=error` unless the provider was only unreachable or failing, `connection_validation_failed` audit event, 401/402/429 recorded on the account |
| Console validate | HTTP 400 page with form error: "Update the private API key...", "The provider is limiting requests...", "The provider's request budget is unavailable..." or a cause-specific message (rejected key, no site found, unreadable answer, unreachable, provider error, refused request) |
| Console clear wait: checkbox missing, changed key or changed delay | HTTP 400 with a field error or the conflict message; a wait that is already NULL reports "There is no provider wait to clear" without an audit event |
| Script | Same kind already attempted today for the binding: `ValueError` before reservation; reservation refusals propagate unchanged; an HTTP, `ValueError` or `ProviderError` failure after reservation marks the attempt `failed` and raises a generic `ProviderError`; an output file write error (`OSError`) propagates unchanged, the attempt stays `uncertain` and the quota and daily guard stay consumed; never retried |

## Rules and invariants

- Lock order: `reserve_account_request` takes only the `ProviderAccount` row lock and counts the ledger inside it, so concurrent workers, validations and scripts serialize on one account budget; `validate_binding` holds the Region row while it reserves. The provider HTTP call happens after commit, never inside the lock. Open-Meteo weather calls in the same job are not ledgered.
- Quota day is UTC (`now.astimezone(UTC).date()`), with the caller's `now` advanced by the monotonic time spent waiting for the lock. Every outcome, including `uncertain`, counts against `daily_quota`.
- `daily_quota` 1-10 is enforced at creation (`create_account`, the console setup form) and by the database; no application path changes `daily_quota` or `enabled` afterwards. An unsupported purpose raises `ValidationError` before the lock.
- Check order: history deadline, credential block, wait, enabled/authorized, quota, then the forecast day bound or the history budget.
- Day bound (`quota._admit_forecast`): a binding starts at most as many forecast jobs per UTC day as it has clocks, however often they are edited. A job counts on the UTC day of its first request, so its retries and the approval job are free; the bound reads the quota of the locked account.
- Forecast budget (`forecasts/budget.py` `remaining_forecast_requests`) is enforced only for `estimated_actuals` reservations with a job. It counts distinct (binding, slot) pairs still needing a request this UTC day: open forecast jobs with no attempt today and due before the day ends, plus future schedule slots, across every approved binding of the account that is validated at the current key revision, has a first forecast job and no disabled schedule. Forecast, validation and script reservations ignore it.
- Forecast clocks are capped at `daily_quota - 1` (one request kept for night history) per region, not per account.
- `record_provider_failure` acts only on 401, 402 and 429, ignores failures from an older credential revision and never shortens a wait. Rotation preserves the wait; only `clear_provider_cooldown` sets it to NULL (expiry does not, but `deferral` ignores a wait in the past), and that leaves the ledger and the credential block untouched. Nothing resets `authentication_failed_revision`; rotation outdates it.
- A throttled `retry` job is woken early only when `request_blocked_until IS NULL` (`jobs.ready.cleared_cooldown`, used by `pending_job_ids` and `claim`): after an administrative reset, or when the account never stored a wait.
- Waits, refusals and skips return the job's execution attempt, so they never exhaust the three-attempt forecast limit; a real 429 response does. History jobs have no attempt limit, only `retry_until`.
- Retry-After (`provider_retry.retry_after_seconds`) is read only for 429 and 503, accepts delta seconds or an HTTP date, and is never shortened; an unparseable value counts as absent and extreme hints saturate at `MAX_RETRY_AT`.

## Tests

- `tests/test_quota_clock.py`, `tests/test_jobs.py` (concurrent reservations, quota until next UTC day), `tests/test_domain.py` (UTC reset, validation consumes quota), `tests/test_provider_identity.py` (canonical accounts share quota), `tests/test_provider_account_invariants.py` (quota and credential state), `tests/test_collection_schedule.py` (forecast and history share the atomic quota), `tests/test_collection_timing.py` (day bound).
- `tests/test_collection_failures.py` (one outcome per answer, 402 block, three 429 answers end a forecast job, lock timeouts), `tests/test_collection_resilience.py` (shared wait, credential block by revision, unexpected errors stay `uncertain`), `tests/test_provider_retry.py`, `tests/test_collection_review_regressions.py` (900 s default, wait reset wakes the throttled job, validation re-arms auth failure), `tests/test_validation_audit.py`, `tests/test_validation_feedback.py` (cause-specific messages).
- `tests/test_provider_recovery.py` (audited, optimistic wait reset through the console), `tests/test_console_step_up.py` (recent confirmation), `tests/test_nightly_solcast.py` (forecast budget versus night history), `tests/test_collection_calendar_windows.py` (15-minute job fails on reserved budget), `tests/test_solcast_capture.py`, `tests/test_admin_setup.py`, `tests/test_collection_status.py`, `tests/test_delivery_health.py`, `tests/test_admin_health_queries.py`, `tests/test_trash_operations.py` (ledger kept on purge).
- Playwright: `web/tests/workspace.spec.ts` "region collection schedule validates and saves distinct local times" asserts that no account counters render, from a mock only.
- Gaps: no test that validation or `check_solcast` reservations may consume budget reserved for forecasts; no test of several regions on one account overcommitting forecast clocks; no browser test of the console wait callout; no budget performance bound with long job history.
