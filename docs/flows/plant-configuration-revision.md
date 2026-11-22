# Edit a plant's name and technical configuration

A workspace administrator saves the plant name and electrical or geometry parameters from the Settings tab (or the `configure_plant` command); a platform administrator does the same from the console details page. The values are one typed `PlantParameters` (`plants/parameters.py`), validated before any transaction opens. One server rule (`operations.plant_edit_block`) refuses the edit of a suspended plant or of an approved plant whose approved connection is not current. While the region is pending or rejected the save records the declaration in the plant's `PlantSubmission`, which approval activates; otherwise it is saved against the current approved binding. A save whose values and binding equal the declaration changes nothing; a name-only change renames the plant and its region; any other change appends an immutable `PlantConfiguration` revision, switches the plant to it, turns automatic calibration off, supersedes unfinished calibration runs and inserts fresh history jobs for a connected EPİAŞ plant. Every change advances `Plant.parameters_revision`, and a save made from an older revision is refused. Viewers see the form read-only.

## Entry points

- Screen: `web/src/features/plant/workspace/settings-section.tsx` `PlantConfiguration`, Settings tab, "Technical details" with the plant CSV download beside it; the form (`TechnicalDetails`) is shown when `plant.parameters` exists, else a notice asks for the details needed for approval. Inputs `features/portfolio/plant-configuration-fields.tsx` `PlantConfigurationFields`, body `configurationInput` plus `expected_revision`; submit via `ActionForm` (`ui/form.tsx`) and `api.put` (`lib/api/client.ts`). The draft and its conflict model live in `plant/workspace/parameters-draft.ts`, the conflict list in `parameter-conflict.tsx` (`ui/changed-fields.tsx`). `features/portfolio/plant-onboarding.tsx` reuses the fields but POSTs (`site-onboarding`).
- `PUT /api/tenants/{t}/regions/{r}/plants/{p}/` -> `pyracast/plants/api.py` `PlantView.put` (`PlantRevisionInput`) -> `operations.revise_plant`. The route answers only `PUT`.
- Console: `GET|POST /admin/plants/{region}/details/` -> `pyracast/platform_admin/plants.py` `plant_details_page` -> `save_plant_details`, which calls `revise_plant(..., expected_revision=plant.parameters_revision, platform=True)` under a version check of the shown details and, for a changed location, `correct_location` (`location-change`).
- Command `configure_plant`: `create_plant`, or `revise_plant_outcome` with `--plant-id` and an optional `--expected-revision`; actor is `--username`. Approval (`activate_submitted_plants`) reuses `_save_configuration`, see `plant-approval`.

## Sequence

```mermaid
sequenceDiagram
    actor A as Admin
    participant UI as TechnicalDetails
    participant V as PlantView.put
    participant O as revise_plant
    participant DB as PostgreSQL
    A->>UI: Save configuration
    UI->>V: "PUT plant URL, values and expected_revision, X-CSRFToken"
    alt not signed in, not an active tenant admin, or CSRF missing
        V-->>UI: "401 not_authenticated, or 403 membership_required, workspace_admin_required, csrf_failed"
    else region or plant missing, trashed or foreign
        V-->>UI: "404 not_found"
    else PlantRevisionInput or PlantParameters invalid
        V-->>UI: "400 invalid_request, fields"
    end
    V->>O: "PlantParameters, expected_revision"
    O->>DB: "BEGIN, lock tenant, region, plant"
    alt suspended, or approved without a current approved binding
        O-->>V: "DomainError plant_edit_blocked"
        V-->>UI: "400 plant_edit_blocked, reason"
    else lock wait over 5 s
        V-->>UI: "503 busy, Retry-After 1"
    else values and binding equal the declaration
        O-->>V: "Plant, nothing written"
    else changed and expected_revision is not the plant's revision
        O-->>V: StaleParametersError
        V-->>UI: "409 plant_parameters_changed, current PlantOutput"
    else region pending or rejected
        O->>DB: "rename when the name differs; upsert PlantSubmission (base = current configuration), event submission_saved"
    else name only
        O->>DB: "plant_renamed, and region_renamed when the region name differs"
    else declared values or binding changed
        O->>DB: "rename, insert revision n+1, repoint plant, automation off, supersede runs"
        O->>DB: "event configuration_created, EpiasCollectionJob rows if connected"
    end
    O->>DB: "parameters_revision + 1 when a value or the name changed"
    V-->>UI: "200 PlantOutput"
    UI->>UI: "reload the draft from the saved plant"
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `accounts_tenant`, `accounts_membership`, `regions_region` | R | Tenant then region locked; membership read by the route policy and `authorize_workspace_admin`; region `status` and `approved_binding` decide the block and the branch |
| `connections_sitebinding`, `connections_provideraccount` | R | `binding_is_current` (region approved at the binding's revision, account enabled and authorized, credential revision) |
| `plants_plant` | R/W | Locked; `name`, `parameters_revision`, `current_configuration`, automation flags and `next_calibration_check_at` |
| `plants_plantsubmission` | W | Upserted while the region is pending or rejected, with `base_configuration` = the configuration current then; it is the declaration while that base is still current (`parameters.declaration`) |
| `plants_plantconfiguration`, `plants_plantevent`, `plants_calibrationrun` | R/W, W, W | New row per changed save, `revision` = previous + 1; events `submission_saved`, `configuration_created`, `plant_renamed` (old and new name), `calibration_automation_disabled` (cause `configuration_revised`, only when automation was on); `pending` and `running` runs become `superseded` (reason `configuration_changed`) with one `calibration_completed` event each |
| `metering_epiasconnection`, `metering_epiascollectionjob`, `forecasts_collectionschedule` | R, W, R | Enabled connection: 90 realtime jobs and, with a settlement plant, 12 settlement months for the new configuration; rows only, dispatched later |
| `regions_region`, `regions_auditevent` | W | A new plant name also renames the region: `region_renamed` |

## Authorization

- DRF: `RoutePolicy` (`api/permissions.py`, `ROUTE_POLICIES`) lists the plant route as `PUT` admin only; it refuses before the view runs and session authentication enforces CSRF.
- View: `ScopedView.plant_queryset` requires a live region and plant of that tenant (404).
- Operation: `authorize_workspace_admin` repeats the administrator check under the tenant lock; the only check for the command. The console passes `platform=True` and requires a platform administrator instead. UI: `ActionForm` disables the fieldset and hides Save without `useCanManageWorkspace` or while `Site.edit_blocked_reason` is set (cosmetic). Database: check constraints on capacities and angles; unique `(plant, revision)`.

## Failure paths

| Condition | Result |
| --- | --- |
| Anonymous | 401 `not_authenticated`, `WWW-Authenticate: Session` |
| Not a tenant member, a viewer, an inactive member | 403 `membership_required` / `workspace_admin_required` |
| Missing CSRF token | 403 `csrf_failed` |
| Region or plant absent, trashed or of another tenant | 404 `not_found` (also when trashed between the view check and the lock) |
| Unknown field, missing `expected_revision` or below 1, missing, blank or over-long name, a name with a NUL character, missing or null capacity, non-numeric or boolean value, capacity 0 or negative, tilt outside 0 to 90, azimuth outside 0 to below 360, angles without fixed mounting | 400 `invalid_request` with `fields` (`message` alone for an unknown field) |
| Suspended region, or approved region whose approved binding is missing or not current (provider account disabled or credentials changed) | 400 `plant_edit_blocked`, the reason's sentence (`EDIT_BLOCK_MESSAGES`); the console shows an operator sentence (`CONSOLE_MESSAGES`) |
| A change saved from another `parameters_revision` | 409 `plant_parameters_changed`, `current` is the plant (`api.errors.current_on_conflict`); the form keeps every edit, lists the fields changed on the server with earlier and current value, keeps a value both sides changed with the server value beside it and names an angle edit dropped after a mounting change |
| Lock wait over 5 s (`lock_timeout`) | 503 `busy`, `Retry-After: 1` |
| Console: the shown details, revision or review status changed | Refused with a reload link; nothing saved |
| Console: location, workspace or plant gone or trashed | 404 |
| Command: invalid value, blocked edit or stale `--expected-revision` | `CommandError` with the validation messages; unknown user, region, plant or access gives one generic message; `--expected-revision` without `--plant-id` or below 1 is refused before anything is read |

## Rules and invariants

- Lock order is tenant, region, plant; `connect_epias` locks region then plant, so the region lock serializes it with a revision. The console details save takes the same locks before its version check, and its version covers the region revision, the review status and the shown values, so a form opened before an approval is refused.
- `PUT` is a full replace: omitted optional fields fall back to null, `net_export` and `unknown`. Numbers are compared as floats with a negative zero held as zero (`parameters.without_negative_zero`), so equal values in another form are no change.
- `expected_revision` applies only to a change: an unchanged save from an older revision succeeds and writes nothing. `parameters_revision` advances with every change of the name or a declared value; a rebind alone (approved binding other than the configuration's) makes a revision without advancing it. The command overwrites without `--expected-revision` and prints the parameters revision it started from under the plant lock and the one it saved, or "unchanged".
- On the configuration branch a revision is made only when a declared value or the binding differs; a name-only save keeps the configuration, calibration, automation and EPİAŞ jobs. A revision starts uncalibrated because activations are per configuration (`forecasting.calibration_at`). A plant rename (`_rename`, on both branches) also renames the region unless it already has that name.
- A plant in review records every technical edit in its submission, also when it was approved before; the submission awaits approval while its `base_configuration` is the plant's current configuration, and `PlantOutput.parameters` shows it, else the current configuration.
- Only `pending` and `running` runs are superseded; earlier activations stay on their revision. Old EPİAŞ jobs are superseded lazily when claimed (`metering/jobs.py` `_current`).
- Validation exists in `PlantInput`, `PlantParameters` (the command and the console reach it too), the constraints and the browser; `PlantParameters` is authoritative and names the refused field. No calculation reads the angles; only tracking mounting changes the transfer (`plants/transfer.py`).

## Tests

- `tests/test_plant_parameters.py` (typed values, stale, concurrent and unchanged saves, edits during review, command revisions), `test_site_states.py` (edit blocks), `test_plant_operations.py` (revision history, geometry, boundaries, identical and name-only saves, submission saves), `test_plant_api.py` (geometry validation, CSRF), `test_admin_plant_console.py` (console details, stale form, blocked save, suspended location correction), `test_plant_commands.py`, `test_site_onboarding.py`, `test_workspace_authorization.py`, `test_calibration_automation.py`, `test_metering_history_backfill.py`, `test_plant_parameters_migration.py`.
- Playwright (mocked API) in `web/tests`: `plant-parameters-draft-model.spec.ts` (conflict draft), `workspace.spec.ts`, `workspace-roles.spec.ts`, `regions.spec.ts`, `plant-onboarding.spec.ts`.
