# Onboard a site: location plus plant submitted for approval

A workspace administrator adds a plant in a three-step wizard (location, technical details, review). One request creates a `pending` region, its plant and a typed technical declaration (`PlantSubmission`) in one transaction. A region that exists without a plant completes setup through the plant endpoint in two steps (technical details, review). Before a new plant the wizard offers to restore a plant from the recycle bin at the entered coordinates, and a refused duplicate location offers to open the plant that holds it. A platform administrator can add the same site for a workspace in the console. The site then waits for `provider-connection-setup` and `plant-approval`; nothing here touches a provider.

## Entry points

- `web/src/features/portfolio/portfolio.tsx` `Portfolio`: the admin-only "Add a plant" button renders `web/src/features/portfolio/plant-onboarding.tsx` `PlantOnboarding` and, on success, refreshes the workspace and opens the new plant. Steps and refusal targets: `onboarding-steps.ts`; coordinates, pasted pairs, zone list and the bin match: `site-location.ts`, `coordinate-field.tsx`, `timezone-field.tsx`. A new plant reads `GET trash/regions/` for the bin offer and restores through `POST regions/{r}/restore/` (`plant-trash-and-restore`).
- `web/src/features/plant/plant-site.tsx` `PlantSite`: a region without a plant (the portfolio's "Complete setup" card) renders `PlantOnboarding` with `existing` for administrators (technical details with the plant name prefilled from the region name, then review; the location is kept and shown); other members see a notice; a location with `edit_blocked_reason` shows that reason to everyone instead. Fields live in `plant-configuration-fields.tsx` `PlantConfigurationFields`, `configurationInput`.
- `POST /api/tenants/{t}/sites/` -> `pyracast.plants.site_api.SitesView.post` (`SiteInput.parameters()`, `SiteInput.location()`) -> `pyracast.plants.sites.submit_site`. Actor: workspace administrator.
- `POST /api/tenants/{t}/regions/{r}/plants/` -> `pyracast.plants.api.PlantsView.post` -> `pyracast.plants.operations.create_plant`.
- Console: `GET|POST /admin/workspaces/{id}/plants/new/` -> `pyracast.platform_admin.plants.new_plant` (`SiteForm`, validated through `SiteInput`) -> `submit_site(..., platform=True)`, then a redirect to the plant's review page (`/admin/plants/{id}/`). Actor: platform administrator.
- Commands: `configure_plant` without `--plant-id` calls `create_plant` for an existing region (actor `--username`; with `--plant-id` it revises, see `plant-configuration-revision`); `fixture_demo` calls `submit_region` and `create_plant`. `submit_region` has no API route. No script or Dramatiq job.
- Reads: `GET sites/` and the portfolio polling are `plant-portfolio`.

## Sequence

```mermaid
sequenceDiagram
    actor A as Administrator
    participant UI as PlantOnboarding
    participant V as SitesView
    participant Op as submit_site
    participant DB as PostgreSQL
    A->>UI: "fill the location step"
    alt a restorable binned plant at the same rounded coordinates
        UI-->>A: "offer Restore name or Add new plant"
        A->>UI: "Restore: POST restore, refresh, open the plant"
    end
    A->>UI: "technical step, review step, per-step validity check"
    UI->>V: "POST sites/ with X-CSRFToken"
    V->>V: "session auth and CSRF, route policy, submission throttle, SiteInput, PlantParameters"
    alt not signed in, not admin, throttled or invalid body
        V-->>UI: "401, 403, 429 or 400"
    end
    V->>Op: "actor, tenant_id, PlantParameters, location"
    Op->>Op: "check location, available zone and name"
    Op->>DB: "BEGIN, lock tenant, check admin and six-decimal location, insert region, audit region_created"
    Op->>DB: "lock tenant again, lock region, check no active plant, insert plant, submission, event"
    alt duplicate location, blocked region or lock timeout
        Op-->>V: "error, whole transaction rolled back"
        V-->>UI: "400 (location_already_exists with region_id) or 503"
        UI-->>A: "earliest step with a refused field opened and focused, or the error notice; Open existing for a held location"
    end
    Op->>DB: "re-read through site_queryset, then COMMIT"
    V-->>UI: "201 SiteOutput with nested plant"
    UI->>UI: "refresh workspace, open /plants/id/forecast/"
    Note over UI,V: "existing region: POST regions/{r}/plants/ to PlantsView and create_plant, technical and review steps, 201 PlantOutput"
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `accounts_membership`, `auth_user`, `accounts_tenant` | R, tenant `FOR UPDATE` | Administrator check (route policy and operation), per-workspace lock |
| `regions_region`, `plants_plant` | R/W | Region insert (`status=pending`, `revision=1`, coordinates rounded to 6 decimals), duplicate probe, `FOR UPDATE` in `create_plant`; plant insert after an active-plant probe, named from the request's `name` |
| `plants_plantsubmission`, `plants_plantevent` | W | `update_or_create` of the technical declaration (later edits by `revise_plant`); event `submission_saved`; `updated_at` is shown as "Last changed" on the review page and decides nothing |
| `plants_plantconfiguration`, `plants_plantevent`, `plants_calibrationrun`, `connections_sitebinding`, `connections_provideraccount`, `metering_epiasconnection` | R/W | Scoped endpoint on an approved region with a current binding only: configuration revision 1, event `configuration_created`, supersede update and EPIAS probe (both empty for a new plant), see rules |
| `regions_auditevent` | W | `region_created` ("Plant submitted") once per region `submit_region` creates, rolled back with a failed site |
| `django_session` | R/W | Session authentication under a per-session advisory lock |

## Authorization

- Authentication: `SessionAuthentication` with CSRF; `deliver` in `web/src/lib/api/lane.ts` sends `X-CSRFToken` on every change.
- Route policy (`pyracast.api.permissions.ROUTE_POLICIES`): `sites/` POST and `regions/{r}/plants/` POST are admin routes, admitted only for an active administrator of `tenant_id`; `sites/` POST is throttled under `submission` (30 per hour and user). `PlantsView.post` then resolves the region in the tenant (`plant_queryset`), so an unknown or trashed region is 404.
- Operation: `authorize_workspace_admin` (`pyracast.accounts.authorization`) in `submit_region` and `create_plant` locks the tenant and re-checks the role; `submit_site` runs both. Only the console passes `platform=True`, which requires an active staff superuser instead of a membership and still locks the tenant. Console POSTs need a recent authenticator confirmation.
- Database: `region_tenant_location_uniq`, `plant_region_uniq`, `plantsubmission_*_chk`, `region_latitude_chk`, `region_longitude_chk`, `region_status_chk`.
- UI: the button and wizard are admin-only (`canManage`) as a convenience only.

## Failure paths

| Condition | Result |
| --- | --- |
| Not signed in | 401 `not_authenticated` |
| Bad CSRF token | 403 `csrf_failed` |
| Not a member of the tenant / member who is not an administrator | 403 `membership_required` / 403 `workspace_admin_required` |
| Over 30 `sites/` POSTs in an hour | 429 `throttled` with `Retry-After` |
| Unknown or missing field, name blank, over 200 characters or holding a NUL character (`regions.operations.require_name`), latitude or longitude out of range, capacity zero or negative, tilt outside 0..90, azimuth outside 0..360 (exclusive), bad enum, angles with non-fixed mounting | 400 `invalid_request`, `fields` keyed by field (an unknown field or a body that is not an object names no field) |
| A zone outside the available IANA zones (`regions.operations.available_zones`) | 400 `invalid_request`, `fields.timezone` |
| Same tenant, same six-decimal coordinates as an active region | 400 `location_already_exists`, body `LocationRefusal` with the holder's `region_id`; the wizard offers to open it |
| Region already has an active plant (scoped endpoint, concurrent submit) | 400 `location_already_has_plant` |
| Scoped endpoint with unknown, foreign or trashed region | 404 `not_found` |
| Scoped endpoint on a suspended region, or an approved one without a current approved binding | 400 `plant_edit_blocked` with the reason, no plant row; the screen shows the reason instead of the wizard |
| Tenant or region lock not granted within 5 s | 503 `busy`, `Retry-After: 1`; a deadlock is 503 `database_conflict`, a statement over 60 s 503 `request_timeout` |
| Another request of the same session holds its advisory lock over 5 s | 429 `session_busy`, `Retry-After: 1`; the browser repeats a request once |
| Console: invalid form, or the operation refuses | 400 page with the field errors; a duplicate location reads "This workspace already has a plant at this location."; a lock failure saves nothing and returns to the console page with a message |
| Any failure inside `submit_site` | Region, audit row, plant and submission all rolled back; the wizard opens the earliest step holding a refused field and focuses its first refused field, else focuses the error notice, which also lists refused fields no step shows (a message that repeats it is shown once); every field is kept |
| A failed restore from the bin offer | The offer stays with the reason and focus returns to its restore button |

## Rules and invariants

- Lock order: tenant row, then (new) region insert, then tenant again (already held) and region row `FOR UPDATE`, then plant insert. The tenant lock serializes concurrent submits; the unique indexes are the backstop (an `IntegrityError` is not mapped and would be a 500).
- Location identity is the tenant plus latitude and longitude rounded half away from zero to six decimals, ignoring trashed regions. Stored coordinates are the rounded values; the bin offer compares with the same rounding (`site-location.ts` `canonicalCoordinate`) and offers the most recently deleted restorable plant at the place, saying how many more share it.
- `_save_plant_parameters` decides the shape: a pending or rejected region stores a `PlantSubmission` and no configuration. An approved region with a current binding gets configuration revision 1 at once, with no plant-approval step; any other region is refused by `operations.plant_edit_block`.
- Through `sites/` and the console one `name` names both region and plant; through the scoped endpoint the plant takes the form's name. A later plant rename records `plant_renamed` and, when the region name differs, also renames the region and records `region_renamed`.
- Plant values are one `PlantParameters` built in the view from `PlantInput` before any transaction, and `submit_region` checks the location, zone and name before it writes, so a bad plant never leaves a region.
- Coordinates are text fields: one decimal comma or point is read as that number ("41,0082" is 41.0082); a second separator, a comma and a point together, a plus sign, a leading zero, an exponent or a value out of range is refused at the field (`coordinatePattern`, `coordinateValue`). A paste into Latitude fills both fields only when the text is a pair: split at a semicolon, a comma followed by white space or white space, at a bare comma only when both parts hold a decimal point or the second starts with a minus, and both parts are values the fields accept; "1.234,5" or "1,234.5" stays in Latitude and is refused there. The console's `admin-workspace.js` applies the same pair rule, and its `SiteForm` reads a coordinate by the same number rule (`platform_admin.plants.coordinate_number`), refusing anything else with "A valid number is required."; its fields offer the full keyboard.
- The zone is a list of the browser's zones (`Intl.supportedValuesOf`) plus UTC and the current value, prefilled from the device zone; a browser without that list gets a text field. The server accepts only `available_zones`.
- Submit and restore run once at a time (`running` ref); a running request disables the form and Cancel, and a wizard closed meanwhile navigates nowhere.
- No idempotency key: a retried POST after a lost response fails with the duplicate-location error instead of duplicating.
- The wizard keeps every step mounted (hidden) so one `FormData` spans all steps. It is read before the fieldset is disabled, because disabled controls drop out of `FormData`.

## Tests

- `tests/test_site_onboarding.py`: combined API, atomic rejection, 403 across tenants, query count of the site list, approval materializing the submission, duplicate coordinates, restore conflicts, concurrent site and plant submit (`Barrier`, operations only), purge. `tests/test_plant_parameters.py` (zones, names, one `region_created` event), `tests/test_onboarding_boundaries.py` and `tests/test_site_states.py` (blocked locations), `tests/test_admin_plant_console.py`: console add (redirect to the review page, field errors, duplicate location, decimal commas).
- `tests/test_plant_operations.py` (`test_invalid_configuration_is_atomic`, `test_tenant_and_binding_boundaries`), `tests/test_plant_commands.py` (`configure_plant` creation), `tests/test_domain_error_codes.py` (`test_site_input_errors_name_the_rejected_field`), `tests/test_workspace_authorization.py` (viewer 403 on `sites/` and `plants/` POST), `tests/test_security_audit.py` (CSRF refusal on `sites/`), `tests/test_api_localization.py` (error shape), `tests/test_route_policy.py` (roles per route), `tests/test_session_serialization.py` (429 `session_busy` on `sites/`).
- `web/tests/site-setup.spec.ts` (steps, refusal focus, zone list, pasted pairs and decimal commas, bin offer, open existing, single submit, blocked locations), `web/tests/site-forms-model.spec.ts` (pair, number and zone rules, shared paste table `web/tests/support/coordinate-pastes.ts`), `web/tests/admin-workspace-submit.spec.ts` (console paste), `web/tests/plant-onboarding.spec.ts` (mocked API: add a plant through `sites/` then approval refresh, retry after error, complete setup); `web/tests/transport.spec.ts` (a change refused twice as busy is reported); `web/integration/workspace.spec.ts` (real backend, complete setup on an approved region, configuration revision 1).
- Gaps: no real-backend browser test of `POST sites/`; no test drives a tenant or region lock timeout or the `submission` throttle through `sites/`.
