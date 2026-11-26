# View a plant's generation forecast

A workspace member (administrator or viewer) opens the plant's Forecast tab in the Generation view, picks a plant-local date, the data ("Forecast" or "Satellite data"), for a forecast Automatic, on a past day Best forecast, or one of the day's forecast times, and a 30 or 60 minute resolution, and the screen polls the plant forecast every 30 seconds. The backend composes the electrical outlook from already saved provider acquisitions, saved forecasts of the day, the active calibration and reported outages, and adds an outage scenario and a production diagnostic where they apply. No domain row is written, no job is enqueued and no provider is called; the read takes no row lock, runs on one read-only snapshot and its only writes are session bookkeeping. The irradiance view of the same tab is `irradiance-forecast-view`.

## Entry points

- Screen: `web/src/features/plant/plant-workspace.tsx` `PlantWorkspace` and `web/src/features/plant/workspace/forecast-section.tsx` `ForecastSection` (date, `DataSourceControl` from `features/forecast/data-source.tsx`, resolution). Date and view are the `date` and `view` URL parameters. "Data" is a two-option radio group, Forecast (default) and Satellite data, sent as `data_source` `forecast` or `satellite`. For Forecast, a "Forecast time" select (`features/forecast/forecast-time.tsx`: Automatic, "Best forecast" for a past day of the generation view, then the day's `selection.options` in the plant time zone; Turkish "Otomatik", "En iyi tahmin") shows when the answer lists options; in Automatic and Best forecast nothing stands beside it and no rule is named; a chosen time shows "Manual" with "Back to automatic". The choice is state of `PlantWorkspace` shared by both views and kept while Satellite data is shown: a chosen time is sent as `forecast_id` and dropped when the date is left (a return to that date opens in Automatic); Best forecast is sent as `selection_mode=best` only by the generation view with Forecast on a past day (the irradiance view then shows Automatic), is kept across past days and turns to Automatic on today or a later day (`keptChoice`). When a read refuses it (400 with the field `forecast_id`, for example after midnight), that view reads and shows the automatic forecast while the other view keeps the chosen time. Choosing keeps the date; a date outside the choice's days shows a notice instead of a request. `web/src/features/plant/workspace/forecast-reading.tsx` `PlantForecast` reads through `useForecastRead` (`useApiQuery`; `ForecastSection` reads the same answer without a request for the options), remounted by a key of view, date, resolution, source, chosen time and configuration id. It polls every 30 s (`cadence.live`) from yesterday on, for both sources; a hidden tab never polls. A pending plant without a configuration renders no `ForecastSection`: its review notice (`web/src/features/plant/review-notice.tsx`) shows instead.
- Rendering: `features/forecast/forecast-panel.tsx` `ForecastPanel` over `web/src/features/forecast/panel/forecast-summary.tsx`, `web/src/features/forecast/panel/forecast-chart.tsx`, `web/src/features/forecast/panel/forecast-unavailable.tsx` and `web/src/features/forecast/panel/model.ts`, plus `forecast-intervals.tsx`. CSV download belongs to `csv-export`.
- Days read: `GET /api/tenants/{t}/regions/{r}/plants/{p}/days/` -> `PlantDaysView` -> `pyracast.plants.plant_days.plant_days`, in one read snapshot with one clock reading: the automatic forecast of each local day from today - 7 to today + 14 at 60 minutes, a past day's by the `day_ahead` choice (`automatic_outlook` in `plants/history.py`, which shares its outage-scenario step with `plant_outlook`), and the production days from today - 30 to yesterday from `plants.production.native_production_days`. Each day carries its total, the rows that have a figure (none when the day has none, at most 52 per day) and, for forecasts, source kind, freshness and the outage figure. A damaged forecast day (`calculation_out_of_range`) is answered as `missing` with that `unavailable_reason`, no rows and one `forecast_read_fault` warning, while every other day is answered; any other error fails the read. The production days come from one selection over the 30 days; when a stored record in it cannot be decoded (an outage revision), each day is selected on its own, and a day whose selection fails is answered without figures (`total_mwh` null, `complete` false, no rows) in the zone of the answered days and with one warning. An outage revision that ended before a day or is cancelled is not decoded further for that day.
- `GET /api/tenants/{t}/regions/{r}/plants/{p}/forecast/?date=&data_source=&forecast_id=&selection_mode=&resolution_minutes=` -> `pyracast.plants.api.PlantForecastView.get` -> `pyracast.plants.history.plant_outlook`, which takes the policy and day window from `pyracast.forecasts.outlook` (`PLANT_POLICIES`, `PLANT_DAYS`) and the forecast choice from `pyracast.plants.forecast_choice.forecast_outlook` with the rules of `pyracast.forecasts.selection`. `data_source` is `forecast` (default) or `satellite`; `forecast_id` names a forecast snapshot and is refused with `satellite` (`ForecastQuery` in `pyracast.api.serializers`); `selection_mode` is `auto` (default) or `best` and excludes `forecast_id` (`PlantForecastQuery`). Actors: workspace administrator and viewer, with identical results.

## Sequence

```mermaid
sequenceDiagram
    actor U as Member
    participant S as GenerationForecast
    participant V as PlantForecastView
    participant O as plant_outlook
    participant DB as PostgreSQL
    U->>S: "choose date, source, resolution"
    loop "on mount and every 30 s"
        S->>V: "GET plants/p/forecast/"
        alt "no session"
            V-->>S: "401 not_authenticated"
        else "not a member"
            V-->>S: "403 membership_required"
        else "forecast budget used up"
            V-->>S: "429 throttled"
        else "location or plant unknown, foreign or trashed"
            V-->>S: "404 not_found"
        else "query invalid"
            V-->>S: "400 invalid_request"
        else "scope and query valid"
            V->>O: "scope and validated query"
            O->>DB: "BEGIN REPEATABLE READ READ ONLY, membership, region, plant"
            alt "window of the policy or cutoff refused"
                O-->>V: "DomainError or ValidationError"
                V-->>S: "400 date_out_of_range or invalid_request (ROLLBACK)"
            else "accepted"
                opt "forecast"
                    O->>DB: "the day's ForecastIssuance rows; today also the fetches from sunrise on"
                end
                alt "forecast, chosen saved forecast or day_ahead (Automatic or Best)"
                    O->>DB: "production total of the day (day_ahead), the shown issuance"
                else "forecast, automatic or chosen later fetch"
                    O->>DB: "current configuration, covering snapshots, activation"
                    O->>O: "compose coverage, elapsed actuals today"
                    opt "today, incomplete after a reference change"
                        O->>DB: "complete issuance of the previous binding"
                    end
                else "satellite"
                    O->>DB: "actual snapshots (today) or archived actual snapshots, configuration, calibration for the day"
                end
                opt "result present and no frozen scenario"
                    O->>DB: "incident revisions known now"
                end
                opt "forecast source kind, no frozen scenario"
                    O->>DB: "latest completed calibration run"
                end
                alt "stored rows damaged"
                    V-->>S: "400 calculation_out_of_range"
                else ok
                    O-->>V: "outlook dict, COMMIT"
                    V-->>S: "200 outlook"
                end
            end
        end
    end
    S-->>U: "chart and notices, empty state by unavailable_reason, or error alert"
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `django_session`, `usersessions_usersession` | R/W | Session load; `SessionLifetime` records activity at most once per 60 s (`last_activity_at`, device record), so most polls only read |
| `accounts_membership` | R | `RoutePolicy`, then `tenant_membership` again inside the snapshot |
| `regions_region` | R | Active region of the tenant; status, revision and approved binding for `binding_is_current` |
| `plants_plant` | R | Plant of that region; `current_configuration_id` drives availability and `previous_configuration`; the view lookup also joins `plants_plantsubmission` |
| `plants_plantconfiguration` | R | Current declaration, the one valid at the day's start, during it, or the first known; the issuance's own |
| `connections_sitebinding`, `connections_provideraccount` | R | Timezone, coordinates, capacity; enabled, authorized and credential revision for `binding_is_current` |
| `forecasts_forecastsnapshot`, `forecasts_collectionjob`, `forecasts_forecastresult`, `forecasts_referencecapacitynormalization` | R | Saved acquisitions of succeeded jobs, collected, available and published by the cutoff; normalization provenance of scaled reference data |
| `plants_forecastissuance` | R | The day's saved forecasts: options, frozen 30/60 minute payloads and outage scenarios of a past day's or a chosen saved forecast and of a reference transition |
| `plants_calibrationactivation`, `plants_calibrationrun` | R | Active or retrospective calibration; diagnostic from the latest completed run |
| `incidents_plantincident`, `incidents_incidentrevision` | R | Latest revision per incident known at the evaluation time, for the scenario and for calibration validity |
| `metering_epiasconnection`, `metering_epiasconnectionrevision`, `metering_epiassnapshot`, `metering_epiascollectionjob` | R | Production identity of a calibration fitted on EPİAŞ data: the current connection on live reads, the revision valid at the cutoff on saved reads |

## Authorization

- Route policy: `ROUTE_POLICIES` in `pyracast.api.permissions` marks this route member access with the `forecast` throttle. `RoutePolicy` runs before the view: no session answers 401, a missing membership 403. Viewer and administrator read alike; there is no role check on this path.
- View: `ScopedView.plant_queryset` loads the region by `tenant_id` and not trashed, then the plant through that region (`get_object_or_404`), before the query is validated. A plant has no trash of its own; it disappears with its location.
- Operation: `plant_outlook` calls `tenant_membership` inside its snapshot, so membership, region, plant and forecast data come from one committed state; a membership removed after the snapshot began is refused from the next poll on.
- Data boundary: no row-level security; tenant isolation is the region filter on `tenant_id` and the plant-to-region join, repeated inside the snapshot. At delivery `CustomerJSONRenderer` (`pyracast.api.public.public_source_data`) drops provider account, site and credential fields, rewrites provider names and passes stored provenance such as `reference_capacity_normalization` only as an allowlist (policy, capacities, factor, time).

## Failure paths

| Condition | Result |
| --- | --- |
| No session, expired session | 401 `not_authenticated` with `WWW-Authenticate: Session` |
| Not a member of the workspace (also one removed before the snapshot) | 403 `membership_required` |
| More than 60 reads a minute in the `forecast` scope (plant, region and performance reads share one budget per user) | 429 `throttled` with `retry_after` |
| Days read: more than 120 an hour (scope `days`), or another session's days read running in the same API process | 429 `throttled`; the second with `Retry-After: 5` and no `api_throttled` event (flow `rate-limiting`) |
| Location unknown, of another workspace or trashed; plant not in it | 404 `not_found` |
| Unknown query key (`as_of` included), missing or bad date, resolution not 30/60, a source other than `forecast` or `satellite`, a blank or repeated `data_source` or `selection_mode` | 400 `invalid_request`; `fields` names the input (an unknown key has a message only) |
| `forecast_id` with `satellite`, or not one of the day's options | 400 `invalid_request`, field `forecast_id`: "Choose one of this day's forecast times." |
| `selection_mode` with `forecast_id`; `best` for today, a later day or `satellite` | 400 `invalid_request`, field `selection_mode` |
| `forecast` after today + 14 or before today - 366 | 400 `date_out_of_range` |
| `satellite` for a future day or a past day older than 366 days | 400 `invalid_request` with "Choose a day before today and within the past year." (localized) |
| A stored record that cannot be decoded where it is read: snapshot intervals (`forecasts.sources.snapshot_rows`: not an array, a row not an object, a bound without offset or unparsable, no positive duration, a power that is a boolean, text or beyond a float; an explicit missing power stays missing), a saved forecast's result or scenario, an outage revision; a figure of valid inputs beyond a float; a chosen saved forecast that cannot be decoded, or a past day whose every saved forecast cannot be | 400 `calculation_out_of_range`; one `forecast_read_fault` warning naming the record (`record=<kind>:<id>`, kinds `forecast_snapshot`, `forecast_issuance`, `incident_revision`) and the class of the failure, never the stored value |
| Another request of the same session running for over 5 s | 429 `session_busy`, `Retry-After: 1`; the client waits at most 2 s and repeats once |
| Statement over 60 s | 503 `request_timeout` with `request_id` |
| Any other error inside the read (for example `local_day_bounds` for a local date the timezone skips) | 500 `server_error` with `request_id`. Read from code, not reproduced |
| No configuration for the day, or the current one failing `configuration_is_current` (account disabled, credentials changed, location revised) | 200, `result: null`, `freshness: missing`, `configuration_unavailable`; empty state "no configuration" |
| No snapshot or issuance | 200, `result: null`, `forecast_unavailable` (empty state "missing is not zero") or `estimated_actuals_unavailable` (its own empty state) |
| Date outside the bounds of the selected source | No request; notice `selectDateInRange` |
| First request fails | The error and Retry in place of the panel |
| Later poll fails | The panel stays, with a "Could not refresh" alert and a retry; polling continues. A 403 or 404 drops the data |

## Rules and invariants

- `plant_policy` picks one policy from source and day (past, today, future), counted from the plant's day in its region's zone (`plants.production.plant_today`); `PLANT_DAYS` bounds the days each policy answers:
  - `day_ahead` (`forecast`, today - 366 .. yesterday): reads the production total (`plants.production.production_total`, `MEASUREMENT_POLICY`, null unless daylight is covered). Automatic (`automatic_selection`) shows the eligible saved forecast (fetched since the previous day's sunrise and not by the approval job) whose daylight day total is nearest to it, compared to the watt-hour (`DISTANCE_DECIMALS`), the later fetch on a tie (rule `closest_to_production`); without a production total the latest eligible one, without an eligible one the latest saved one (rule `latest`); otherwise the missing answer. Best forecast (`best_selection` through `forecast_choice.best_forecast`, also the figure of `forecast-performance`) lets every saved forecast of the day compete, the approval fetch included: the nearest daylight total, the later fetch on a tie, without a production total the latest one. Nothing is stored.
  - `automatic` (`forecast`, today .. today + 14): `automatic_forecast` composes the newest covering snapshots for the current configuration (`latest-forecast-coverage-v2`); today adds elapsed estimated actuals (source kind `mixed`), and an incomplete day after a reference change falls back to the previous binding's complete issuance.
  - `live_actuals` (`satellite`, today): `current_forecast` with completed actuals only.
  - `archived_actuals` (`satellite`, today - 366 .. yesterday): `plants.satellite_data` (`satellite_data(...).day`, `satellite_result`, also the figure of `forecast-performance`) reads the archived actual snapshots, with no saved-forecast fallback. It prefers the current configuration when its binding archived the day, with the calibration whose training period had begun (`calibration_timeline(...).for_day`, `retrospective_calibration`) while its production identity is current; otherwise the declaration valid at the day's start, else one made during it, else the first known, each day counted in that declaration's own location's zone, at factor 1.
- Forecast options (`forecast_choice._day_forecasts`): the plant's `ForecastIssuance` rows of the day (`forecast_archive.saved_issuances`), newest fetch first; today adds every fetch of the current binding collected or published from the day's sunrise on, so a fetch collected before sunrise and published after it is offered today and never for a past day. Every saved forecast is decoded before it is ranked or offered (`plants.forecasting.decoded_issuance`); one that cannot be decoded logs one `forecast_read_fault` warning (step `candidates`) and is neither ranked nor offered, so the day answers from the readable ones. Every forecast answer carries `selection` (`mode` `auto`, `best` or `manual`, `rule` `closest_to_production`, `latest` or null, `forecast_id`, `options` with `forecast_id` and `fetched_at`); satellite answers carry null. An automatic answer for today or a later day names the newest fetch the shown result's provenance names when that fetch is an option, else null.
- A shown saved forecast, automatic, best or chosen, is its frozen issuance (`issuance_outlook`), never recalculated, flagged `archived_forecast`, and `previous_configuration` when its configuration is not the plant's `current_configuration_id`; a chosen later fetch of today is composed only from itself and fetches collected before it, without satellite data.
- The read runs in one `REPEATABLE READ, READ ONLY` transaction (`pyracast.db.read_snapshot`) and takes no row lock: it never waits for a writer and never delays one. Membership, configuration, snapshots, calibration and incidents come from one committed state, so the normal result and the outage scenario see the same incidents. A write committed during the read appears from the next poll on.
- The envelope (`PlantForecastOutput`, shared `ForecastEnvelope`) carries `freshness`: `fresh` for a live result, `stale` with `stale_reason` `collection_overdue` when a contributing snapshot was collected over 24 h ago, `archived` for a completed day and for frozen issuances, `missing` without a result. `source_kind` is `forecast`, `estimated_actuals` or `mixed`.
- Outage scenario: every issuance result carries the `outage_scenario` frozen at collection (possibly null), is never re-evaluated and gets no diagnostic; only the reference-transition fallback drops it. Otherwise `incident_scenario` evaluates incidents known now and returns the scenario with an optional replacement result; the outlook is not mutated. A calibration whose training-window incident history changed is dropped at selection (`uncalibrated`); when the change is only visible at evaluation time (reference transition) the result is recalculated at factor 1 with `calibration_invalidated_by_outage_history`.
- A tracker plant computes under the geometry version of its active calibration (the default when uncalibrated) and records it in `result.model_version`; the scenario and any recomputation keep that version, and a calibration fitted under another transfer model is not applied.
- `production_diagnostic` only for `source_kind: forecast`, no frozen scenario, a day not yet ended and a latest completed run whose derating baseline equals the outlook factor and whose transfer model equals the outlook `model_version`; evidence whose latest matched day is more than two days before today is `stale`.
- Defaults differ: API `data_source=forecast`, `resolution_minutes=30`; screen Forecast in automatic mode, 60, tomorrow in the plant time zone, which the plant header states once ("Plant time: UTC+3"; in the forecast, production and comparison views the offset of the days on screen, "UTC+1 / UTC+2" across a clock change).
- Rendering: the heading shows one data line, "Forecast data: {time}" (the `fetched_at` of the option `selection.forecast_id` names, else the newest valid collection time of the data shown), when one is known and, when `freshness` is `stale`, the not-current sentence above it: "Newer forecast data was not available.", or for an inactive connection (`stale_reason` `connection_inactive`) "No new forecast data is arriving for this plant." with the contact address. An answer without a result shows "No forecast data for this day.", "No satellite data for this day." or "No technical details for this day."; a day without daylight (`polar_night`) shows "No daylight on this date."; otherwise a result that is not complete shows "Data is missing for some hours." and no total. One outage notice: "Outage impact is unknown." plus, when the technical details changed and every outage has at least one capacity field, "Technical details changed. Check the capacity in the outage record.", otherwise for a workspace administrator "Enter the capacity still in service in the outage record." Daily totals and MW show two decimals, W/m² whole numbers, with a non-breaking space before the unit; the table is named "Hourly values" or "Half-hourly values" from the result's `resolution_minutes`, or from its interval lengths when it has none. Quality flags are not shown.

## Tests

- API and operation: `tests/test_plant_api.py` (unavailable shape, 400 for resolution 15, resolution capping), `tests/test_archived_forecast_api.py` (a saved forecast replayed, also one published after collection), `tests/test_api_localization.py` (Turkish completed-day message), `tests/test_archived_actual_composition_api.py`, `tests/test_plant_forecasting.py`.
- Dispatch and freshness: `tests/test_outlook_dispatch.py` (every source, day and cutoff answers as pinned, plant and region), `tests/test_forecast_freshness.py`, `tests/test_forecast_read_faults.py` (damaged actuals or issuance answer 400 with one warning), `tests/test_forecast_read_cost.py` (cost does not grow with history), `tests/test_forecast_contract.py`.
- Forecast choice: `tests/test_forecast_choice.py` (options of a past day and of today, the total nearest to production, the approval fetch, a chosen saved forecast or later fetch, a damaged saved forecast neither ranked nor offered, Best forecast against Automatic and where it is refused, refused choices, a blank or repeated `data_source`, both reads counting past days in the region's zone, `as_of` and the `data_source` values `auto`, `archived_forecast` and `estimated_actuals` refused), `tests/test_forecast_selection.py` (the rules).
- Selection policy: `tests/test_current_forecast.py`, `tests/test_current_forecast_coverage.py`, `tests/test_automatic_forecast_selection.py`, `tests/test_intraday_outlook.py`, `tests/test_forecast_after_midnight.py`, `tests/test_historical_outlook.py`, `tests/test_past_day_outlook.py`, `tests/test_archived_forecast.py`, `tests/test_outlook_preparation.py`, `tests/test_provisional_calibration.py`, `tests/test_production_diagnostics.py`, `tests/test_incident_calibration.py`, `tests/test_historical_production_identity.py`, `tests/test_transfer_replay.py`.
- Days read: `tests/test_plant_days.py` (window across midnight and daylight-saving days, each day equal to its single-day read, refused and damaged days, a damaged outage revision refusing only the days it reaches, rows only with figures, access, the `days` budget and lock, query cost).
- Access: `tests/test_route_policy.py` (policy table, each role), `tests/test_api_throttling.py` (429 `throttled`, one budget per user), `tests/test_session_serialization.py` (busy session), `tests/test_public_data.py` (provenance reaches customers as an allowlist).
- Snapshot: `tests/test_forecast_read_locks.py` (a row lock held elsewhere does not delay the read; one read-only snapshot without `FOR UPDATE`; incident, publication, revision, suspension, account and membership writes commit while a calibrated read is open; the open read keeps its source, factor and scenario, and the next read shows the change), `tests/test_db.py` (`read_snapshot`).
- Contract: `tests/test_forecast_read_contract.py` (viewer and administrator bodies equal, foreign plant and region 404, `invalid_request` field on unknown key and empty date, a past day's forecast is its saved forecast).
- Playwright (mock API): `web/tests/workspace.spec.ts` (30 s refresh, data sources and archive, missing configuration, edge-zero cropping), `web/tests/query-layer.spec.ts` (a failed poll keeps the chart, a hidden tab makes no request), `web/tests/forecast-date-bounds.spec.ts`, `web/tests/forecast-panel-model.spec.ts`, `web/tests/forecast-time.spec.ts` (Automatic, a chosen time, Best forecast on past days), `web/tests/daylight-forecast.spec.ts`, `web/tests/forecast-intervals.spec.ts`, `web/tests/forecast-coverage.spec.ts`, `web/tests/midnight-forecast.spec.ts`, `web/tests/outages.spec.ts`; real API: `web/integration/workspace.spec.ts`.
- Gaps: no test of this read for an error that answers 500.
