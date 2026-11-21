# Edit a plant's name and technical configuration

A workspace administrator saves the plant name and electrical or geometry parameters from the Settings tab (or the `configure_plant` command); a platform administrator does the same from the console details page. While the region is pending or rejected and the plant has no configuration, the save overwrites the customer's `PlantSubmission`; otherwise it needs a current approved binding. A save whose parameters and binding equal the current configuration changes nothing but the name; any other save appends an immutable `PlantConfiguration` revision, switches the plant to it, turns automatic calibration off, supersedes unfinished calibration runs and inserts fresh history jobs for a connected EPİAŞ plant. A new name renames the plant and its region. Viewers see the form read-only.

## Entry points

- Screen: `web/src/features/plant/workspace/settings-section.tsx` `PlantConfiguration`, Settings tab, "Technical details" (shown when a configuration or submission exists); inputs `features/portfolio/plant-configuration-fields.tsx` `PlantConfigurationFields`, body `configurationInput`; submit via `ActionForm` (`ui/form.tsx`) and `api.put` (`lib/api/client.ts`). `features/portfolio/plant-onboarding.tsx` reuses the fields but POSTs (`site-onboarding`).
- `PUT /api/tenants/{t}/regions/{r}/plants/{p}/` -> `pyracast/plants/api.py` `PlantView.put` -> `operations.revise_plant`. The route answers only `PUT`.
- Console: `GET|POST /admin/plants/{region}/details/` -> `pyracast/platform_admin/plants.py` `plant_details_page` -> `save_plant_details`, which calls `revise_plant(..., platform=True)` under a version check of the shown details (a stale page is refused with a reload link) and, for a changed location, `correct_location` (`location-change`).
- Command `configure_plant`: `create_plant`, or `revise_plant` with `--plant-id`; actor is `--username`. Approval (`activate_submitted_plants`) reuses `_save_configuration`, see `plant-approval`.

## Sequence

```mermaid
sequenceDiagram
    actor A as Admin
    participant UI as PlantConfiguration
    participant V as PlantView.put
    participant O as revise_plant
    participant DB as PostgreSQL
    A->>UI: Save configuration
    UI->>V: "PUT plant URL, X-CSRFToken"
    alt not signed in, not an active tenant admin, or CSRF missing
        V-->>UI: "401 not_authenticated, or 403 membership_required, workspace_admin_required, csrf_failed"
    else region or plant missing, trashed or foreign
        V-->>UI: "404 not_found"
    else PlantInput invalid
        V-->>UI: "400 invalid_request, fields"
    end
    V->>O: "validated values, _validate_configuration before BEGIN"
    O->>DB: "BEGIN, lock tenant, region, plant"
    alt invalid value, or no current approved binding
        O-->>V: ValidationError
        V-->>UI: "400 invalid_request"
    else lock wait over 5 s
        O-->>V: "OperationalError (LockNotAvailable)"
        V-->>UI: "503 busy, Retry-After 1"
    else region pending or rejected and no configuration
        O->>DB: "rename when the name differs; upsert PlantSubmission and event submission_saved when a parameter differs"
    else binding current, parameters and binding unchanged
        O->>DB: "rename only when the name differs: plant_renamed, and region_renamed when the region name differs"
    else binding current, parameters or binding changed
        O->>DB: "rename, insert revision n+1, repoint plant, automation off, supersede runs"
        O->>DB: "event configuration_created, EpiasCollectionJob rows if connected"
    end
    O-->>V: "Plant (success branches)"
    V-->>UI: "200 PlantOutput"
    UI->>UI: "store savedPlant, tabs remount on new configuration id"
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `accounts_tenant`, `accounts_membership`, `regions_region` | R | Tenant then region locked; membership read by the route policy and `authorize_workspace_admin`; region `status` selects the branch, `approved_binding` supplies the binding |
| `connections_sitebinding`, `connections_provideraccount` | R | `_binding`, `binding_is_current` (region approved at the binding's revision, account enabled and authorized, credential revision) |
| `plants_plant` | R/W | Locked; `name`, `current_configuration`, automation flags and `next_calibration_check_at` |
| `plants_plantsubmission` | W | Upserted only while the region is pending or rejected and the plant has no configuration; kept after approval |
| `plants_plantconfiguration`, `plants_plantevent`, `plants_calibrationrun` | R/W, W, W | New row per changed save, `revision` = previous + 1; events `submission_saved`, `configuration_created`, `plant_renamed` (old and new name), `calibration_automation_disabled` (cause `configuration_revised`, only when automation was on); `pending` and `running` runs become `superseded` (reason `configuration_changed`) with one `calibration_completed` event each |
| `metering_epiasconnection`, `metering_epiascollectionjob`, `forecasts_collectionschedule` | R, W, R | Enabled connection: 90 realtime jobs and, with a settlement plant, 12 settlement months for the new configuration; rows only, dispatched later |
| `regions_region`, `regions_auditevent` | W | A new plant name also renames the region: `region_renamed` |

## Authorization

- DRF: `RoutePolicy` (`api/permissions.py`, `ROUTE_POLICIES`) lists the plant route as `PUT` admin only; it refuses before the view runs and session authentication enforces CSRF.
- View: `ScopedView.plant_queryset` requires a live region and plant of that tenant (404).
- Operation: `authorize_workspace_admin` repeats the administrator check under the tenant lock; the only check for the command. The console passes `platform=True` and requires a platform administrator instead. UI: `ActionForm` disables the fieldset and hides Save without `useCanManageWorkspace` (cosmetic). Database: check constraints on capacities and angles; unique `(plant, revision)`.

## Failure paths

| Condition | Result |
| --- | --- |
| Anonymous | 401 `not_authenticated`, `WWW-Authenticate: Session` |
| Not a tenant member, a viewer, an inactive member | 403 `membership_required` / `workspace_admin_required` |
| Missing CSRF token | 403 `csrf_failed` |
| Region or plant absent, trashed or of another tenant | 404 `not_found` (also when trashed between the view check and the lock) |
| Unknown field, missing, blank or over-long name, missing or null capacity, non-numeric or boolean value, capacity 0 or negative, tilt outside 0 to 90, azimuth outside 0 to below 360, angles without fixed mounting | 400 `invalid_request` with `fields` (`message` alone for an unknown field) |
| Not (region pending or rejected, and no configuration) and no current approved binding: suspended, location change or new binding awaiting approval, provider account disabled or credentials changed | 400 `invalid_request`, "An approved current provider binding is required." |
| Lock wait over 5 s (`lock_timeout`) | 503 `busy`, `Retry-After: 1` |
| Command: bad user, region, plant, access or value | `CommandError`, one generic message; a lock timeout escapes as `OperationalError` |

## Rules and invariants

- Lock order is tenant, region, plant; `connect_epias` locks region then plant, so the region lock serializes it with a revision. The submission branch needs `region.status` pending or rejected and no configuration.
- `PUT` is a full replace: omitted optional fields fall back to null, `net_export` and `unknown`. On the configuration branch a save makes a revision only when a saved column differs (the seven parameters or the binding, compared after `_binding` succeeds); an identical or name-only save keeps the configuration, calibration, automation and EPİAŞ jobs. A revision starts uncalibrated because activations are per configuration (`forecasting.calibration_at`). A plant rename (`_rename`, on both branches) also renames the region unless it already has that name; `create_plant` does not synchronize names. On the submission branch an unchanged submission is kept with its `submitted_by` and no `submission_saved` event.
- Only `pending` and `running` runs are superseded; earlier activations stay on their revision. Old EPİAŞ jobs are superseded lazily when claimed (`metering/jobs.py` `_current`).
- No expected-revision check on the API or command: concurrent admins overwrite each other. The console details page alone compares a version of what it showed.
- Validation exists in `PlantInput`, `_validate_configuration` (the command reaches no serializer), the constraints and the browser; the operation is authoritative. No calculation reads the angles; only tracking mounting changes the transfer (`plants/transfer.py`).

## Tests

- `tests/test_plant_operations.py` (revision history, invalid input, geometry, boundaries, identical and name-only saves, submission saves), `test_plant_api.py` (geometry validation, CSRF), `test_admin_plant_console.py` (console details), `test_site_onboarding.py`, `test_workspace_authorization.py`, `test_calibration_automation.py`, `test_metering_history_backfill.py`, `test_plant_commands.py`.
- Playwright (mocked API) in `web/tests`: `regions.spec.ts`, `workspace.spec.ts`, `workspace-roles.spec.ts`, `plant-onboarding.spec.ts`. Gaps: no concurrent `revise_plant` test; no browser test of a save error.
