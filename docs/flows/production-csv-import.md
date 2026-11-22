# Import production from a CSV file

A workspace administrator picks a CSV of completed 30 or 60 minute energy intervals on the Production tab. The browser validates it, the API previews it against the current values and the administrator confirms; the confirmed batch is saved as new immutable observation revisions that override provider values and request a calibration run. The `import_production` command is an older insert-only writer that shares the table but not the rules.

## Entry points

- Screen: `web/src/features/production/production-import.tsx` `ProductionImport`, mounted by `web/src/features/production/production-history.tsx` `ProductionHistory` only when `canManage` and a current configuration id exist. Parser: `web/src/lib/production.ts` `parseProduction`, which takes its messages from the `ProductionCsv` translator of `ProductionImport`.
- `POST /api/tenants/{t}/regions/{r}/plants/{p}/production/preview/` -> `pyracast.plants.api.ProductionPreviewView` -> `production_changes.preview_production`.
- `POST .../production/changes/` -> `ProductionChangesView` -> `production_changes.change_production` (the commit step; shared with inline edits in `production-history`).
- Command `import_production` (`--username --plant-id --configuration-id --unit --file`) -> `production_changes.import_observations`. There is no REST endpoint for it.

## Sequence

```mermaid
sequenceDiagram
    actor A as Admin
    participant UI as ProductionImport
    participant V as API views
    participant P as production_changes
    participant DB as PostgreSQL
    A->>UI: "choose CSV, parseProduction validates, an invalid file stops here"
    A->>UI: "Preview import"
    UI->>V: "POST preview/ with the file's unit and observations"
    V->>P: "preview_production"
    P->>DB: "read snapshot without row locks: check administrator, read heads, provider snapshots, incidents, write nothing"
    alt not administrator or invalid body or configuration not current or rule broken
        V-->>UI: "401, 403, 404 or 400 invalid_request"
    else ok
        V-->>UI: "changes with expected_revision_id, preview rows, warnings"
    end
    A->>UI: "Confirm import (with an acknowledgement when a value exceeds capacity)"
    UI->>V: "POST changes/ with the echoed changes, unit, origin csv_import, file name and SHA-256"
    V->>P: "change_production"
    P->>DB: "lock tenant, region, plant, read heads, compare expected_revision_id"
    alt head moved since preview
        V-->>UI: "409 production_revision_changed"
    else value above capacity without confirmation
        V-->>UI: "400 production_implausible"
    else ok
        P->>DB: "retire old heads, insert revisions, PlantEvent, request preparation"
        V-->>UI: "200 created, existing, count, ids"
        UI-->>A: "Import complete notice, history reloads"
    end
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `accounts_tenant`, `accounts_membership`, `regions_region`, `plants_plant` | R (locked on commit only) | Administrator check, active rows, writers serialized |
| `plants_plantconfiguration`, `connections_sitebinding`, `connections_provideraccount` | R | Configuration must be current (`configuration_is_current`) on the web path and for new command rows; binding zone converts local boundaries |
| `plants_productionobservation` | R/W | Current heads read; commit sets `is_current=false` on replaced heads with one statement and inserts the revisions with one (`supersedes`, `recorded_by`) |
| `plants_plantevent` | W | `production_changed` (web) or `observations_imported` (command), only when rows were created |
| `plants_plant` (`calibration_preparation_due_at`) | W | `request_preparation` marks the plant in the transaction; one attempt follows the commit and the dispatcher's calibration phase prepares what is left, which may add a `pending` `plants_calibrationrun` |
| `metering_epiasconnection`, `metering_epiassnapshot`, `metering_epiascollectionjob`, `incidents_*` | R | Preview current values and provider-hour warnings |
| `forecasts_forecastsnapshot` | R | Command estimated-actuals coverage (`reference_intervals`) |

## Authorization

- View: `SessionAuthentication` and the route policy (`pyracast.api.permissions.ROUTE_POLICIES`): both routes need an active membership with the administrator role and share the `production_import` throttle (120 per hour per user). Then `ScopedView.plant_queryset` loads the active region of the tenant and the plant of the region.
- Operation: `tenant_membership(role=ADMIN)` again; the commit takes it with the tenant row lock, so it serializes with administrator reassignment. Preview needs it too, without locks.
- Browser: the section is hidden unless `useCanManageWorkspace()`; viewers have no CSV path (the inline editor sends their single edits to `change-requests/production/`, flow `change-request-submission`).
- Command: acts as any `--username`; the operation check decides. Database constraints (`productionobservation_*`) guard shape and overlap, not roles.

## Failure paths

| Condition | Result |
| --- | --- |
| File over 2 MiB (checked on `file.size` first), wrong header, parse error, 0 or over 2000 rows, naive timestamp, length not 30 or 60 minutes, future end, bad energy, quality `mixed` (an aggregated slot of a production export), other bad quality or source, overlap inside the file | Error notice (`ProductionImport.choose_a_csv_no_larger_than_2` for size, otherwise a `ProductionCsv.*` message for the first problem only), nothing sent |
| Not signed in | 401 `not_authenticated` |
| Viewer; foreign tenant; bad CSRF | 403 `workspace_admin_required`; 403 `membership_required`; 403 `csrf_failed` |
| Unknown region or plant; unknown configuration id or one of another plant | 404 `not_found` |
| Body fails the serializer (naive timestamp, boolean or negative energy, quality, empty or over 2000 rows, unknown field, unknown action, `set` without energy) | 400 `invalid_request` with `fields` (an unknown top-level field has a message only) |
| Configuration not current (revised, region changed, provider account disabled or unauthorized); command checks it only for rows it would insert | 400 `invalid_request`, "requires the current approved configuration" |
| Not on the local half-hour grid, crosses local midnight, first and last local day 31 or more days apart, overlaps another row or a non-reverted head, head of a previous configuration, revert without a head | 400 `invalid_request`, message without row number |
| Commit with a value above 1.25 times the plant's power ceiling and no `confirm_implausible` | 400 `production_implausible`, field `changes` |
| Head changed between preview and commit | 409 `production_revision_changed` |
| More than 120 requests in an hour | 429 `throttled` |
| Commit lock wait over 5 s | 503 `busy`, `Retry-After: 1` |
| Statement over 60 s | 503 `request_timeout` |
| Preview or commit request fails | Alert above the form or `ActionForm` error; the old preview table stays visible after a failed commit |
| Command: no estimated actuals for the interval | "Observation import failed: Matching historical estimates are unavailable. Choose a day with collected data." |
| Command: any other validation or permission refusal | `CommandError` "Observation import failed: " plus the refusal message; unreadable file, header, row, unknown user or plant gives "Observation import failed: invalid file, input, access, or record." |

## Rules and invariants

- Commit lock order is tenant, region, plant. Preview runs in one `REPEATABLE READ`, read-only transaction (`pyracast.db.read_snapshot`) and takes no row lock, so it does not wait for a writer.
- Optimistic concurrency: preview returns `expected_revision_id` per row (null if no head); commit rejects the whole batch on any mismatch. A commit sent again after it was applied is a replay: it answers like the first call and changes nothing. Rows equal to the head (energy, quality, source, reverted flag) count as `existing`: no revision; with no created row there is no event and no calibration request.
- kWh is divided by 1000 on arrival. The energy column names the unit (`energy_kwh` or `energy_mwh`); the preview echoes `energy` in that unit and the commit sends the same unit. The parser also requires `starts_at`, `ends_at`, `quality` and `source` and allows `timezone`, so a production export imports again unchanged.
- A value whose mean power exceeds 1.25 times the lower of DC capacity and power limit is warned in the preview (`energy_exceeds_capacity`); the screen then requires an acknowledgement before it sends `confirm_implausible`. The event details record `origin: csv_import`, `file_name` and `file_sha256`.
- Manual rows of any quality suppress the whole provider hour; the preview warns (`provider_hour_incomplete`) only when a provider hour is partly covered.
- Command path: no revision tokens, no 31-day limit, no capacity check; an overlap with any current head must match it exactly (interval, configuration, energy, quality, source) and must not be reverted, or the call conflicts; such a duplicate counts as existing before the currency check, so it succeeds even with a no-longer-current configuration; new rows need a current configuration and matching estimated actuals. The command accepts a CSV with `starts_at`, `ends_at`, `energy` and optional `quality` and `source` (defaults `valid`, `operator`), only a `.csv` file under the working directory, at most 2 MiB and 2000 rows.

## Tests

- Backend: `tests/test_production_changes.py` (stale and overlap batches, preview tokens, configuration boundaries, API permissions including viewer 403 on preview and changes, concurrent winner), `tests/test_production_writes.py` (replay, preview without row locks, capacity confirmation, event details), `tests/test_native_production.py` (provider-hour warnings, invalid batches, preview then commit through the API, command overlap rules), `tests/test_plant_commands.py` (command replay, path, header, size and row guards, specific refusals), `tests/test_plant_operations.py` (command operation), `tests/test_workspace_authorization.py`, `tests/test_routed_surface.py` (no `observations/` route).
- Playwright on `mockApi`: `web/tests/workspace.spec.ts` (import, stale file reads, `parseProduction`), `web/tests/exports.spec.ts` (an export imports again, `mixed` refusal), `web/tests/epias-production.spec.ts` (preview, unit by column, import over an open draft, capacity acknowledgement, Turkish import review), `web/tests/localization.spec.ts`, `web/tests/responsive.spec.ts`. Against live Django: `web/integration/workspace.spec.ts` exports a day and imports it again (preview, confirm, one already present).
- Gaps: no UI test of a failing preview or commit request, nor of the `future` and size messages.
