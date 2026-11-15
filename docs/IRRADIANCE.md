# Irradiance contract

## Target

Reconstruct **Solcast's implied horizontal irradiance** from Hobby PV forecasts and integrate it into the next local day's incoming solar energy. Output interval GHI in W/m² and daily energy in kWh/m², optionally MJ/m². Incoming solar energy is different from electrical generation.

The reference for reconstruction is Solcast GHI from the same forecast issue, location, interval and configuration. A satellite estimate or another provider's forecast does not establish equivalence to Solcast. Later physical measurements support a separate forecast-accuracy evaluation.

## Provider inputs

- Start with one authorized horizontal reference site per region: `tilt=0`. Record AC/DC capacity, azimuth, losses, installation date, coordinates, timezone and available snow/shading behavior. Confirm the saved settings and actual response schema. Do not assume 1 kW means 1 m² or that all losses can be disabled.
- The current case reference uses 1000 kW AC and 1000 kW DC, `tilt=0`, `azimuth=0` and a 100% efficiency factor (`losses_percent=0`). Store the verified AC normalization capacity as `capacity_kw` and module nameplate as `dc_capacity_kw`; unknown historical DC capacity remains null. Changing provider settings requires a new binding revision, validation and approval. Preserve older capacities and snapshots; plant configurations must explicitly adopt the new binding before collection and calibration resume for them.
- Hobby endpoints: `/rooftop_sites/{resource_id}/forecasts` and `/rooftop_sites/{resource_id}/estimated_actuals`. The current public contract documents 30-minute intervals, up to 10 requests per UTC day per account, two nearby arrays, forecasts through 14 days and estimated actuals for seven days. Represent entitlement explicitly and verify it for each authorized account.
- Archive each acquired forecast vintage. Estimated actuals are not archived forecasts. Retain raw payload, collection time, provider issue time when available, interval bounds, units and configuration revision. Never invent an unavailable issue time.
- Normalize source-specific units and timestamp conventions at the adapter boundary. Confirm central/quantile semantics rather than assuming the central value is P50 or an expectation.

Live forecast collection requests the smallest whole-hour horizon reaching the
midnight after today and the following two local dates. Normalize only that
three-date window, retaining the full provider response. Local calendar boundaries
determine the horizon across daylight-saving changes; a boundary interval keeps
its native bounds when its mean is prorated.

At each forecast slot and during nightly recovery, check completed native half-hours
within the rolling 168-hour archive. Require local hours overlapping daylight,
including whole sunrise/sunset boundary hours for meter comparisons; polar night
requires none. Complete coverage consumes no request. Otherwise the established
`hours` parameter requests the shortest whole-hour lookback reaching the oldest gap,
capped at 168 hours. This contiguous response can repeat intervening archived data
and nights; Hobby's public guide does not specify arbitrary `start`/`end` filtering.
Future forecast requests retain their account-wide UTC quota reservations before
history is admitted.

## Estimation

Implement a replaceable estimator with typed inputs and outputs containing values, method/version and quality flags.

1. Start with `G_proxy = 1000 * P_kw / capacity_kw`, explicitly marked **unvalidated** and using verified capacity semantics. It is a baseline, not a physical identity.
2. If the actual Solcast forward model and its inputs are available, invert it only on a unique, well-conditioned domain and check the forward residual.
3. Otherwise compare a small regularized/appropriately monotone calibration against the baseline using matched Solcast labels. Fit offline and deploy versioned artifacts; do not train inside requests or automatically replace a model after every refit.

Solcast publicly describes an empirical AC model involving irradiance and air temperature. A PVWatts/NOCT/SAPM surrogate is not its verified inverse. Unknown losses, clipping and time aggregation can prevent exact recovery. Temperature or wind features must be available at forecast issuance and improve held-out results before adoption.

For the 1000 kW AC reference, 600 kW of interval-mean source power gives a 600 W/m² proxy; a half-hour contributes 0.3 kWh/m². Continue dividing by each source binding's recorded capacity, including older 1 kW revisions. The 100% efficiency factor does not disable the empirical temperature response or dynamic snow losses.

Preserve these limits:

- `P90-P10` is forecast uncertainty, not identified within-period variance. A spread feature is optional; do not invent a Jensen correction from it.
- Do not apply an assumed IAM divisor or universal solar-angle cutoff. Invalid low-sun inversion is not zero irradiance.
- Missing, nonfinite, negative or out-of-domain data require explicit quality handling. Daytime zero PV does not always imply zero GHI. Treat night and partially sunlit intervals explicitly.
- Do not silently substitute external GHI into the reconstructed result or advertise interval/daily accuracy without measurement.
- Tilted-roof POA and DNI/DHI recovery are later extensions. The two-array isotropic inversion is not Solcast's documented Reindl inverse.

## Daily energy

Use the region's IANA timezone to obtain tomorrow's local midnight boundaries, then convert them to UTC. For interval-mean irradiance:

`energy_kwh_m2 = sum(ghi_wm2 * overlap_seconds) / 3_600_000`

`energy_mj_m2 = energy_kwh_m2 * 3.6`

A constant 600 W/m² for 30 minutes contributes 0.3 kWh/m². Partial-interval prorating assumes the interval mean represents the overlap. Reject or explicitly reconcile duplicates and overlaps. Use union coverage; do not assume 48 intervals or convert missing daylight to zero.

Return partial energy, coverage and completeness separately. Initially require valid coverage of the entire local day, including night, for a complete total; otherwise return a null daily total and the available partial energy. Any later astronomical night-filling policy must be explicit, flag inferred values and preserve partially sunlit intervals. Sum only compatible intervals from one selected vintage, or disclose an explicit mixing policy. Do not label summed interval P10/P90 values as daily quantiles.

Forecast projections additionally report a separate `daylight` summary using the
shared `astral-3.2-sea-level-v1` windows. Its completeness and coverage require valid
values during astronomical daylight. Its `whole_native_intervals` energy scope
retains the whole source intervals overlapping those windows, clipped only to the
selected local day: sunrise and sunset do not divide an interval mean into invented
day/night energy. Original native boundaries determine this scope even after
forecast-composition or outage splits, and electrical limits apply before aggregation.
The energy total additionally requires valid values throughout those selected native
boundary intervals; a missing boundary fragment can leave the total null even at
100% astronomical daylight coverage. The summary is independent of the requested
30/60-minute display resolution.
Daylight source coverage and valid-value coverage remain separate. Irradiance
`input_coverage_seconds` and `input_coverage_fraction` report astronomical daylight
covered by finite, nonnegative source PV power, including valid zeroes. They use
the actual selected interval pieces, excluding null values and gaps; original
native bounds do not fill coverage. These fields are absent from electrical and
outage summaries and the fraction is null during polar night. Existing
`coverage_fraction` still reports usable derived GHI, so complete input coverage
does not establish a complete irradiance total. Zero reference power with unknown
sunlight still cannot establish GHI. Missing daylight values
give a null daylight total and an available partial estimate. Polar night has no
daylight total or coverage fraction. The frontend opens this scope, while existing
full-day fields, saved outputs and raw intervals keep their original meaning; no
missing nighttime values are filled or classified from this summary.

## Validation and output

Primary metrics are daily kWh/m² MAE and signed bias against matched Solcast forecasts. Also measure daylight GHI error, low-sun behavior and coverage. Use chronological day blocks and held-out locations for geographic claims. Avoid relative errors near zero and leakage from later observations. A few weeks can verify the pipeline; they do not establish seasonal validity.

Return region/date/timezone, surface, interval bounds and GHI, daily/partial energy, completeness, source vintage, collection time, binding/model revision and quality flags. Tests cover unit conversion, local midnight/DST, missingness, overlap, solver validity and selection of information available at issuance. Keep the pure calculation core free of network/database access.

## Live auxiliary weather

Only Hobby endpoints are used for Solcast acquisitions. With `PYRACAST_WEATHER_ENABLED=1` (the default), forecast worker deliveries additionally acquire Open-Meteo `temperature_2m`, `snow_depth`, `snowfall` and `shortwave_radiation` for the source coverage window. No additional Solcast entitlement or request is required. Fixture providers and estimated-actual acquisitions do not trigger this auxiliary request.

Open-Meteo `shortwave_radiation` is a separate model's GHI forecast. Preserve hourly means as `[timestamp-1h, timestamp)`; temperature and ground snow depth are instantaneous endpoint values, while snowfall is a preceding-hour sum. Request UTC Unix timestamps, preserve native hourly resolution, and integrate elapsed overlap with the region's IANA calendar day. Missing values and gaps give partial energy with a null complete total. Never split an hourly mean into purported measured half-hours.

Persist the successful Hobby acquisition before requesting auxiliary weather. Archive the raw auxiliary response, request coordinates/window, acquisition time and failure separately on that snapshot before publication. Calculation retries reuse the saved acquisitions without new network calls. An interrupted auxiliary request leaves an explicit unavailable reference; a reclaimed worker publishes the pinned primary acquisition without consuming Hobby quota again. A stale worker cannot attach weather after losing its lease. The response's `generationtime_ms` is execution duration, not issue time; leave issue time null. The daily projection includes native intervals, requested/grid coordinates, a payload digest, coverage and source-specific acquisition time. Auxiliary failures do not block publication of an otherwise valid Hobby result; they produce an unavailable weather reference until a subsequent acquisition.

The result's `weather_reference` is displayed separately from the reconstructed regional proxy and plant electricity. It never fills or replaces their missing values, trains plant calibration, or becomes a matched Solcast GHI label. Ground snow depth and snowfall do not identify panel snow-loss percentage. The conditional quadratic inverse remains unavailable without a verified snow-loss input; external temperature does not inherit the historical Solcast-weather validation result. This integration makes no claim of improved forecast or on-site accuracy.

Open-Meteo's free endpoint supports evaluation/non-commercial use. Set `PYRACAST_OPEN_METEO_API_KEY` for an authorized commercial customer endpoint; the adapter does not fall back to the free endpoint after a customer-request failure. Keys remain outside raw snapshots and HTTPX request logs. Display the required Open-Meteo and CC BY 4.0 attribution next to this data, including a processing note. Existing Hobby CSV downloads and reminder emails continue to contain only their original dataset, not the auxiliary weather reference.

Primary contracts: [Open-Meteo forecast fields](https://open-meteo.com/en/docs), [attribution](https://open-meteo.com/en/licence), and [access plans](https://open-meteo.com/en/pricing).

## Offline conditional reconstruction

`scripts/calibrate_irradiance.py` pairs Historical Rooftop PV `PT30M` power with six contiguous `PT5M` irradiance/weather intervals. Match explicit UTC bounds, not array positions or filename geometry. Required weather fields are `ghi`, `air_temp` and `snow_soiling_rooftop`; the tilted `gti` field is not the horizontal reference. The operator must establish matching coordinates, horizontal geometry, capacity and loss settings. Unmatched intervals remain counted, never filled.

The fitted forward approximation is `F(g,t) = c0 + c1*g + c2*t + c3*g*t + c4*g² + c5*t²`, with `g = GHI/1000`, `t = (air_temp_c-25)/25`, capacity `C` in kW and snow transmission `q = 1-snow_loss_fraction`. Fit `P/C` against the native five-minute means of `q*[1,g,t,g*t,g²,t²]` using scaled QR with reorthogonalization and rank checks. Fit only positive-power intervals whose six native GHI values exceed 25 W/m² and transmissions are at least 0.05, outside the low-light zero-output domain. These are explicit engineering support guards, not universal physical thresholds or confidence levels.

For application with half-hour mean weather, set `a=c4`, `b=c1+c3*t`, `d=c0+c2*t+c5*t²`, and `y=P/(C*q)`. The ascending solution is:

`GHI = 1000 * 2*(y-d) / (b + sqrt(b² - 4*a*(d-y)))`

This rationalized expression also handles a linear model without cancellation. Require a positive derivative throughout the declared GHI support domain and a finite supported root; check the forward residual. Return the local sensitivity `dGHI/dP = 1000/(C*q*(b+2*a*g))`. Reject missing weather, opaque or poorly transmitting snow, known capped power and unsupported roots. Never infer clipping solely because power equals or exceeds nameplate. The 1400 W/m² support limit is an implementation domain, not a physical upper bound. Flag temperatures outside the fitted range. Zero power is not identifiable unless a caller supplies an explicit night classification; the offline workflow does not derive night from target GHI.

Five-minute variance and covariance are not available from a half-hour mean. In general `mean(F(g,t))` differs from `F(mean(g),mean(t))`, and varying snow introduces further covariance. Consequently this is a **conditional point approximation**, not an exact inverse or a rigorous irradiance bound. Native GHI is used to fit and evaluate, never as an inverse input. Temperature and snow must be available at the relevant acquisition/forecast time. Without them, the conditional model is unavailable; the live Hobby adapter continues to expose the separate unvalidated power/capacity baseline.

The CLI freezes the first 60% of complete local days for training, uses the next 20% for validation and the remainder for testing, and never refits on the test period. It writes `model.json`, `metrics.json` and `predictions.csv`, with source SHA-256 digests, coefficient version, support limits, chronology, missingness and per-interval flags. Its daily metric is **partial energy over the same identifiable intervals** for both methods; it does not validate complete daily totals or future forecasts. The input files and generated case artifacts remain local.

The authorized 2026-11-11 case paired 17,376 half-hours. Istanbul time has 361 complete local days and two partial boundary days. Training used 216 days (2025-11-06 through 2026-06-09), validation 72 and testing 73 (2026-08-21 through 2026-11-01). On 1,713 identifiable test intervals, GHI MAE was 1.3002 W/m² versus the baseline's 50.4214 W/m²; RMSE was 6.7675 W/m². Daily partial-energy MAE was 0.007262 versus 0.589119 kWh/m² on the same intervals. The test set contains no snow-covered intervals and 277 inverse estimates extrapolate beyond the fitted temperature range. The largest daily partial-energy error was +0.175536 kWh/m² on October 26; October 25 was +0.081967. Both remain in evaluation. These results establish historical model-output agreement at this site, not measured irradiance accuracy, seasonal universality or forecast equivalence.

Run the reproducible case from the repository root. Both inputs must be existing
JSON files whose canonical paths remain inside the command's working directory.
Library callers can supply an explicit trusted root.

```text
uv run --locked python scripts/calibrate_irradiance.py --power docs/json_37.80375_34.460913_fixed_0_0_1000_1.00_PT30M.json --weather docs/json_37.80375_34.460913_fixed_33_180_PT5M.json --capacity-kw 1000 --timezone Europe/Istanbul --output .codex/irradiance-model
```

## Plant electrical generation

Keep incident solar energy (kWh/m²) separate from plant electricity (MWh). The initial
`reference-power-transfer-v1` model uses normalized reference PV power directly:

`raw_power_mw = reference_power_kw / reference_capacity_kw * plant_dc_capacity_mw`

`plant_power_mw = min(factor * raw_power_mw, applicable_limit_mw)`

The factor starts at 1. This transfer is an **unvalidated proxy**, not a PVWatts
physical model or a claim that reference AC capacity equals plant DC capacity.
Plant geometry, temperature and individual losses are not inferred from the
reference site. A physical replacement needs appropriate irradiance/weather inputs,
plant geometry and verified improvement on unseen days.
Plant configuration can record unknown, fixed or tracking mounting, with optional
fixed-array tilt and azimuth (clockwise from true north). These are declared metadata;
the reference-transfer model does not use them. Tracking does not imply a known axis,
backtracking policy or row geometry. Never infer plant geometry from a downloaded
weather filename or treat a grid export limit as an inverter rating.

### Tracking geometry transfer

One number cannot scale a horizontal reference to a tracking plant: the plane-of-array gain
depends on solar position and on the beam share. For a `tracking` configuration whose
reference binding is horizontal (`tilt=0`) with a declared DC capacity,
`tracking-geometry-transfer-v1` multiplies each native reference interval by a physical gain
before calibration and clipping:

`raw_power_mw = reference_power_kw / reference_capacity_kw * plant_dc_capacity_mw * gain`

At each interval midpoint: estimate GHI from loss-free reference power per DC capacity with
`p = 0.9964*g - 0.1484*g²` (`g = GHI/1000`, fitted on the authorized horizontal case and
used only for the clearness index); split it with the Erbs diffuse fraction; rotate a
north–south horizontal axis with ground coverage ratio 0.40, a ±55° limit and slope-free
backtracking; transpose with Hay–Davies and albedo 0.20; divide plane irradiance by GHI and
apply −0.35 %/K over 29 W/m²K to the plane excess for warmer tracker cells. Astral supplies
solar position at sea level. Zero reference power, zenith of 87° or more, or GHI of at most
1 W/m² give gain 1, and the ratio is capped at 6. These are versioned assumptions flagged
`assumed_tracker_geometry`: declared axis, row spacing, bifacial gain and terrain are not
modeled. Other configurations keep `reference-power-transfer-v1` and `geometry_not_modeled`.
The pure-Python gain agrees with an independent pvlib 0.16.1 chain within 0.012 on the
tested cases.

Evidence from the authorized case, 2025-11-05 to 2026-11-03, against hourly settlement
energy: a weekly rolling origin refit the factor on the trailing 30/60/90 days and scored the
next seven. Direct transfer gave interval MAE 9.39/9.68/9.71 MW, daily MAE 79.4/82.6/86.0 MWh
and factors from 0.99 to 1.73. With the gain: 6.97/6.78/6.40 MW, 61.3/59.2/55.1 MWh and
factors from 0.83 to 1.00. Morning, midday and afternoon factors fitted separately were
1.48/0.95/1.34 on direct transfer and 0.89/0.89/0.88 with the gain, where they no longer
improved unseen days. Using Solcast DNI/DHI instead of the reference-derived split changed
in-sample MAE by 0.2 MW. One scalar therefore remains the calibrated quantity; time-of-day
and seasonal factors are not fitted. This is transfer accuracy against estimated actuals,
not day-ahead forecast accuracy. A calibration records its `transfer_model` and is neither
applied, compared nor activated under another one. Outputs saved earlier keep their recorded
model and factor, and their outage scenarios replay that model.

Known limit: the horizontal rooftop reference includes dynamic snow soiling. In the case
year 57 days were affected, with reference power near zero on days when the plant produced
280–530 MWh. The gain cannot restore that energy; such days stay large underestimates until
a validated snow-free source is added.

For inverter AC production, apply the known inverter limit. For net export, apply
the smaller known inverter/export limit. Omit unknown limits rather than inventing
them. Thus 130 MW DC and a 100 MW export limit are separate inputs. Apply clipping
at the native source interval **before** hourly aggregation: native powers 80 and
120 MW over two half-hours with a 100 MW limit produce 90 MWh for the hour.
Interval means cannot recover clipping at finer time resolution.

Explicit forecast outlooks compose native forecast intervals under `latest-forecast-coverage-v2`.
The newest acquisition owns its coverage window, including internal missing values;
older acquisitions can fill only the remaining windows. Split native intervals at
ownership boundaries before calibration, clipping and aggregation. Retain their
mean values and original bounds, and flag `uniform_mean_proration` when a split
assumes a uniform mean over a shorter duration. Preserve every contributing source
and apply the same pieces to normal and outage scenarios. This composition remains
a forecast and does not change day-ahead replays. Today's automatic outlook and
email instead prefer saved native Solcast estimated actuals for completed native
intervals, using `estimated-actuals-then-forecast-v1`. Actual acquisitions follow the
same coverage ownership rule before valid pieces replace forecasts; nulls and gaps
inside newer actual coverage cannot recover older actual values. The original
native end must have elapsed, even when acquisition clipping shortened the saved
row. Remaining time uses saved forecasts. Expose `mixed` source identity and every
contributor; these actuals remain model estimates, not EPİAŞ production or meters.
Once actuals cover astronomical daylight and its whole native boundary intervals,
select actuals alone. Missing nights remain missing. Explicit actuals expose the
available current-day history without forecast filling.

Today's automatic reference-transition fallback selects one complete original
pre-daylight issuance under the compatibility rules in `ARCHITECTURE.md`. Preserve
that source binding's normalization capacity and label the selection explicitly.
Past-day automatic views select Solcast estimated actuals and preserve missing
history as unavailable rather than substituting forecast power.

Reported outage scenarios split native intervals at the reported boundaries.
Scale the uncapped reference transfer by remaining DC/nameplate DC, then apply
the calibration factor and the smaller applicable normal/remaining capacity cap.
An export constraint applies only to a net-export target. Disconnected modules
must be reflected in remaining DC; reducing only the inverter cap does not model
their lost collection area. This is a conditional availability approximation,
not a component-level electrical simulation. An unknown impact or an unconfirmed
configuration change produces missing affected power. Explicit zero availability
can establish zero electrical output even when reference power is missing.

Integrate power over UTC overlap durations; preserve 23/25-hour local days. Missing
power gives a null complete total with separate partial energy and coverage.
Electrical zero is valid even when it cannot establish zero GHI. Do not derive
plant power by filling ambiguous reconstructed GHI. Return the selected source,
configuration, model, calibration and quality flags alongside both electrical and
separate regional irradiance results.

## Production calibration

Fit one positive multiplier against metered interval energy, preserving native
clipping inside the objective. Use duration-weighted Huber loss on power residuals
normalized by DC capacity, with delta 0.05, an identity penalty of 0.001 and a factor
domain [0.25, 2]. The one-dimensional solver considers every clipping breakpoint
and each convex segment; it does not assume the entire capped objective is convex.
These are versioned engineering defaults, not universal statistical constants.

Policy `regularized-huber-v7` uses completed local days with complete, matched
astronomical daylight coverage and `valid` observations. Select whole meter
intervals with positive overlap with daylight; keep sunrise/sunset boundary slots
and their full energy without prorating. Valid measurements must cover every
daylight window, and reference data must cover each entire selected meter interval.
Exclude days containing daylight gaps, invalid records, outages or declared curtailment.
Night readings are optional and excluded from this scope, never synthesized as zeros.
Do not infer outages from low output alone. Exclude measurements at or above 98% of the known cap from
training because clipping hides uncapped performance; retain valid clipped intervals
in evaluation. Complete valid zero-production and zero-source days remain in the
chronological split and holdout scoring, including their errors. A training day
must contain positive measured energy with positive reference power; otherwise
exclude it from factor learning and report zero-production training-day counts.
Zero-source samples do not identify a multiplicative factor.
An incident overlapping any part of a meter interval excludes that entire interval;
the complete-day coverage rule can consequently exclude the day. Pin immutable
incident revision IDs in the dataset. Later corrections preserve old fit evidence
but invalidate its use until a new eligible fit is reviewed. Incident effects are
not folded into the permanent performance multiplier.
The policy also requires five training days with at least two
hours of positive native source power that is not already clipped at the minimum
factor. Each supported day must have normalized slope-squared exposure of at least
0.02 hours: sum the sample duration times the squared ratio of its uncapped power
slope to DC capacity, capping that ratio at one. For example, two hours at 10% of
DC capacity reach this threshold. These conservative support thresholds prevent a
fit based on one useful measurement; they are engineering guards, not confidence
levels or a requirement for distinct power values in a one-parameter model.

One complete valid matched day can prepare an analysis job. With one to six days,
the result is `insufficient`: retain production diagnostics without fitting or
activating a factor. At least seven complete matched days are required for fitting.
For 7–29 days, reserve the last max(2, ceil(20% of days)) for evaluation; for 30 or
more, reserve the last seven.
Fit only earlier days and do not refit on the holdout. Seven days can check the
pipeline and support a **provisional** fit; a month covers more conditions but
neither duration proves seasonal validity. Intervals within a day are correlated,
so hundreds of half-hours are not hundreds of independent weather realizations.
Archive twelve completed months of settlement injection initially and 90 days of
provisional reported production. Preserve every acquisition and its source identity.
The automatic matching pool contains at most 60 calendar days ending on the latest
recorded completed day. Training uses the valid days within the 30 calendar days before
the final holdout (`recent-calendar-days-v1`); older days never enter the fit, and the
holdout is never refitted. On the authorized case with the tracking transfer, weekly
origins scored on the same evaluation days gave interval MAE 5.58, 5.60, 5.58 and 5.62 MW
for 30-, 45-, 60- and 90-day windows, 5.88 MW for 14 days and 5.75 MW for 180 days; the
30-day window had the lowest daily MAE (46.2 MWh). The monthly mean of the 30-day factor
moved between 0.86 and 0.94 through the year, so one recent month follows that drift
without a window search. A factor describes its training period onward: completed days
before that period are not shown with it.
Year boundaries and matching last year's calendar dates have no special weight.
Retain operating regimes, capacity changes, meter definitions and known incidents;
older commissioning data is useful context, not evidence for today's full capacity.
External historical weather files are not used by this reference-power calibration.

Report interval power MAE/bias (MW), per-day daylight-interval energy MAE/bias (MWh),
included/excluded days, censored counts and the exact chronological split. These
energy sums are not 24-hour totals. Preserve `metric_scope`, astronomical policy
and UTC daylight windows in the dataset manifest; compare all factors on identical
meter intervals. A Huber candidate is `ready` only when both holdout interval MAE
and daily MAE improve by at least 1% over factor 1 and the fitted factor is inside
the allowed bounds. Neither MAE may worsen against the incumbent factor, and
absolute power/energy bias may not worsen against either
comparator (apart from numerical tolerance). Pin the incumbent activation when the
job is created and retain it in the dataset manifest. Activation fails if the current
selection has changed since that comparison. Old policy candidates cannot be newly
activated; existing activation history remains valid. Rejected candidates cannot
be activated; the current activation remains unchanged.
If the Huber candidate fails these guards and the incumbent differs from 1,
evaluate the explicit `identity_recovery` candidate, factor 1. It must improve both
holdout MAEs by at least 1% over the incumbent, beyond numerical tolerance, without
worsening either absolute bias. It equals the factor-1 baseline rather than beating
it. Preserve the fitted Huber factor and metrics and identify the selected candidate
kind. This recovery option retains the same training-support, chronological,
source, authorization and activation guards; it never resets an aging factor merely
because time has passed or assumes factor 1 is correct.
For either candidate kind, any complete holdout day with zero measured energy and
positive matched reference power rejects the candidate with
`zero_production_holdout`. Record its count as `zero_production_holdout_days`,
retaining the full holdout dates, errors and independent diagnostics. Such days
score operational prediction error but cannot authorize a new transfer factor.
A day with both zero source power and zero measured energy does not trigger this
guard. Rejection also prevents manual activation.
The 1% margin is an operational acceptance rule, not a significance test. Confidence
intervals are not estimated in this version; do not expose invented probability
bands. Fitting and activation remain separate transactions. An authorized member can
enable automatic calibration before an eligible fit exists. Daily durable checks and
new acquisitions prepare fits without provider calls from the calibration worker.
Automatic promotion requires at least 30 matched days, seven held-out days, and at
least 1% improvement in both MAEs against the pinned incumbent, together with the
candidate-specific baseline and bias guards above. The latest holdout day must be
within seven completed calendar days and all seven must lie within the last
fourteen days. These are operational
freshness guards, not statistical confidence limits; an old native cohort cannot
be promoted simply because a recent historical import triggered another fit.
After a promotion, require seven entirely new holdout dates
before another promotion. Rejection, insufficient evidence, revoked authority or a
changed source/configuration/incident/incumbent preserves the last good selection.
Disabling automation keeps that selection; resetting to baseline also disables it.
Configuration changes disable the old authorization. Compare later unseen days and
revisit performance across seasons before making accuracy or degradation claims.
With at least 30 matched days, report up to three expanding-training, three-day
evaluation folds within the training period. The final seven-day holdout is never
included in these folds. They diagnose historical reference-transfer stability;
they neither tune hyperparameters nor establish operational forecast accuracy.
Report source-positive interval MAE separately, using whole meter intervals with
positive reference power; it is not an astronomical daylight classification.

Every analysis reports independent production evidence under
`strong-unclipped-power-diagnostics-v2`, including insufficient or rejected fits.
Its reference is the pinned eligible active calibration, explicitly identified by
`reference_kind`, or uncalibrated factor 1 when none is eligible. The newly fitted
candidate is never its own reference, and neither reference establishes a healthy
plant. Anchor the ten-calendar-day search window on the latest complete valid
matched day, retaining observed zero-production days. Keep whole meter intervals
whose reference prediction is at least 25% of DC capacity and where neither
prediction nor measurement reaches 98% of the cap. A day qualifies with at least
three such hours.

Report each qualifying day's measured/predicted ratio, hours, `shortfall` or
`within_threshold` status, and whether all matched meter intervals in the selected
metric scope had zero measured energy. One or two qualifying days remain visible
even though the aggregate assessment is `insufficient`. With at least three
qualifying days, a median ratio
below 0.92 retains the guard status `suspected`; otherwise it is `normal`. Also
report the latest matched date, that day's status and ratio; a latest day without
enough qualifying hours is `insufficient`, not evidence of recovery. Daily evidence
can therefore show a recent drop or improvement while the longer median differs.
Show the actual qualifying dates and day count. The median is not total production
loss, a fault diagnosis or a capacity estimate; these thresholds are engineering
guards, not confidence levels.

Current/future forecast outlooks expose this evidence as `production_diagnostic`
when its configuration, source identity, incidents and reference factor remain
applicable. Evidence older than two completed local days is labeled `stale`.
The diagnostic changes neither forecast power nor DC/inverter/export capacity.
Only reported operating limits drive the separate outage scenario. A `suspected`
aggregate assessment pauses automatic promotion of fitted Huber candidates over an
active calibration. Explicit `identity_recovery` may pass that diagnostic veto only
after all source, freshness, thirty matched-day, seven new holdout-day, paired MAE,
bias and zero-production holdout guards pass. Its reference comparison cannot
identify the cause of a persistent production change or establish healthy capacity.
Manual activation retains its existing guards. Reported outage intervals remain
excluded from calibration. Unreported partial losses can still affect training,
and a first fit cannot distinguish their cause from normal model-transfer bias.
Later candidates, including identity recovery, must pass the unchanged paired
holdout and activation gates. Recording an incident remains necessary when its
operating history is known.

The dataset pairs manual metered production or settlement injection with matching Solcast estimated actuals,
never archived forecasts. Prefer complete current Hobby acquisitions under the
source-selection policy below, applying the request cutoff before ranking, and
retain the snapshot, source kind, collection/publication times,
configuration and dataset digest. Pin the selected reference and settlement
snapshot IDs when requesting a new run; later acquisitions cannot enter its
retries, even with a backdated timestamp. Existing legacy runs retain their
recorded cutoff policy. Only completed local days enter calibration;
future production intervals are rejected at import. Importing an old measurement
explicitly associates it with the declared configuration, without backdating that
configuration. Do not mix periods with different capacities or meter definitions.

An authorized historical rooftop PV export is another delivery of the same modeled product,
imported under a settings manifest matching the approved binding. Policy
`native-thirty-days-else-settings-matched-pool-v2` fits on native Hobby days alone once
thirty complete matched days exist. Until then it pools native days with that export, so a
new binding does not restart from one week of data; each day still uses exactly one source.
Legacy unknown products remain a separate cohort and are never pooled. If no cohort has
seven days, keep the fit insufficient. Record the pooled products, coverage counts and
excluded samples; preserve the full pinned source manifest for audit.
Import historical exports with an explicit settings manifest matching the
approved binding, preserve native timestamps/means and the original payload digest,
and record the actual import/publication time. Never infer settings from the filename
or backdate availability. Automatic activation additionally needs all seven final holdout
days from the current binding's native Hobby estimated actuals or explicitly authorized
capacity-normalized copies of that product, and at least 30 selected training and holdout
days that are such days or the settings-matched historical export. Recompute this gate
from the immutable per-day source manifest; historical exports alone cannot establish this
source-transfer evidence.
Declared configurations applied to older readings remain an explicit retrospective
assumption. Neither source supplies measured irradiance labels.

For a confirmed capacity-only change, use `normalize_reference_capacity` with
explicit source/target binding IDs and an operator authorization reference. Archive
derived copies under the current binding using `P_new = P_old * C_new / C_old`.
Keep the original snapshots, binding capacities and raw provider payload unchanged;
the normalization record identifies the raw payload's original capacity, both
snapshot IDs, the scale, operator and declaration. Both bindings must declare DC capacity
with equal DC/AC ratios: an undeclared value leaves the provider's implicit ratio in
effect. Other model inputs must match.
Never convert Historical Rooftop exports into Hobby records or normalize a copy
again. Preserve acquisition/issue times, set derived availability/publication to
the actual conversion time, and recalculate saved results at the target capacity.
This does not rewrite previous forecasts, calibrations or metered production.
Legacy snapshots without an availability timestamp qualify only when an existing
saved result proves publication before conversion; preserve the unknown original
availability and publish the derived copy at conversion time.
Native complete acquisitions precede authorized scaled copies, which precede
historical exports. Calibration reports count native and normalized support
separately; normalization creates no additional observed days or API requests.

The authorized November 2026 comparison confirmed why both bindings need declared DC
capacity: the original 1 kW Hobby site with undeclared DC capacity returned 1.25 times
the power of the 1000/1000 kW site on 116 common daylight intervals. Its scaled copies
were not equivalent. The plant owner's
[announcement](https://smartsolar.com.tr/HaberDetay.aspx?Id=5520) places full-capacity
operation in March 2026, so January 1 is not a verified full-capacity boundary.
Longer history and seasonal testing are supported by the
[IEA forecasting evaluation guidance](https://www.iea-wind.org/wp-content/uploads/2022/06/IEAWind_Task36_Recommended_Practice_Part2_2nd_Edition_public.pdf)
and [IEA PVPS solar-resource handbook](https://iea-pvps.org/wp-content/uploads/2024/09/Best-Practices-Handbook-for-the-Collection-and-Use-of-Solar-Resource-Data-for-Solar-Energy-Applications-Fourth-Edition-.pdf);
they do not remove the need for local chronological and source-specific validation.

The initial seven-day provider window can contain fewer than seven matched local
days. Wait for sufficient support rather than relaxing the minimum. Archived
native actuals use `native-estimated-actuals-coverage-v1`: compose eligible native
acquisitions under one binding in newest-first coverage order, including windows
acquired during the selected day. Older acquisitions fill only outside newer
coverage; invalid or missing newer values remain missing. Do not mix native,
capacity-normalized and imported products. Preserve a complete authorized whole
source when native daylight coverage is insufficient. A complete native daylight
archive takes precedence despite missing nights; numerical totals and calibration
still require their whole native and meter boundary intervals. Preserve elapsed
23/25-hour days, finite nonnegative power, and explicit missingness independently
of whether reconstructed GHI is identifiable.

Calibration pins `actual-composition-v1` source IDs, coverage segments and exact
native pieces at its cutoff. Replay verifies that immutable acquisitions reproduce
the pin; later acquisitions cannot enter a pending fit. Existing integer pins and
completed fits retain their original selection. Preparation fingerprints compare
effective selected-day values and product identity, so equivalent whole and split
native acquisitions deduplicate while product changes remain significant. Night
collection and bounded retries follow the saved-data collection policy above.
Already active factors remain historical selections; ready candidates from
unsupported older policies require a new request before activation. Old forecast-based
candidates cannot be newly activated as this calibration. Existing activation
history remains readable. Activation applies only after its recorded time, so
archived forecasts retain the factor available then. Holdout improvement against
estimated actuals does not establish improvement in future forecast accuracy.

Production entry is independent of archived weather availability. Calibration requires matching archived estimated actuals for the entire interval.
Saving at least one day with complete valid matched daylight coverage automatically
schedules analysis; one to six days produce diagnostics with an insufficient fit.
Fitting still requires seven complete days and sufficient informative training
support, and automatic promotion requires at least thirty matched days. Daylight
gaps and outage labels do not satisfy complete-day coverage.
Use a 60-day matching pool ending on the latest recorded local day and elapsed durations, including DST
days. Capture both an observation timestamp cutoff and an ID watermark so readings
saved during fitting cannot enter that dataset, even with identical timestamps.
New readings remain saved and can prepare a subsequent candidate. Preparation never
activates a factor; the separately authorized promotion policy does.

The versioned `astral-3.2-sea-level-v1` policy uses the apparent upper solar limb,
standard refraction and a sea-level horizon, without terrain or shading inference.
Collect UTC sunrise/sunset events around the local day to preserve DST, date-line
and midnight-crossing daylight. Polar day requires the full local day; polar night
does not count as a calibration day. The journal and calibration use the same
windows. A time interval ending exactly at sunrise or starting at sunset has no
daylight overlap. The journal hides night slots but retains original saved readings.

## Archived forecast evaluation

The live current/future outlook can change at every collection time. Archived
forecast evaluation instead scores the immutable electrical issuance saved before
that target day's first astronomical daylight. Its selection policy is
`latest-published-pre-daylight-issuance-v1`, shared with the archive screen.
Acquisition and publication must both precede that cutoff; a known provider issue
time must not follow acquisition. A pre-dawn same-day collection can replace the
previous evening's forecast. Daytime forecasts and estimated actuals cannot.
Polar day, polar night and midnight-crossing daylight use local midnight.
The saved forecast horizon follows collection: its local day and the following
two days. Intervals beyond that configured horizon are not archived.

The performance endpoint evaluates 1?90 completed local days. Optional
`issuance_cutoff` is an offset-free local time on the preceding day and selects
only issuances available by that earlier time; ambiguous or nonexistent local
times are rejected. The latest eligible acquisition wins even when partial.
Missing native intervals remain missing; no fallback invents complete coverage.

Mark this as `archived_published_forecast`. Use the issuance's frozen calibrated
and factor-1 native power vectors, including the original clipping, without
applying today's transfer model or calibration again. Select measurements under
that issuance's configuration. Exclude a day as `configuration_not_comparable`
when its measurement basis or timezone differs from the requested comparison;
never aggregate incompatible quantities. Legacy raw snapshots and read-time
caches do not establish that an electrical output was published then and are not
promoted into issuances retrospectively.

With `daylight_only=true`, require complete daylight coverage and report
`metric_scope: daylight_intervals`; this is the frontend default. The API's
full-day mode retains full local-day coverage and metrics. Both modes preserve
whole meter boundary intervals. Keep valid zero-production and zero-source days
in evaluation, including their errors. Reported incidents do not remove otherwise
valid production from archived forecast scoring; explicit invalid/outage-quality
readings remain excluded. Do not infer outages from zero alone. Source-positive
MAE is null when no such intervals exist.

Return daily measured/predicted/baseline MWh, signed error, issuance, snapshot,
configuration, calibration and model identities, selection cutoffs, excluded-day
reasons and aggregate MAE/bias. Return requested, matched, missing and excluded
day counts plus scored interval/hour coverage. Missing days cannot become zero
errors. This diagnostic does not train or activate a model and does not produce
confidence intervals from a short or selectively observed history.

The separate `historical_model` comparison uses saved estimated actuals available
at evaluation time and the currently declared configuration. Each day uses the newest
activated calibration whose training period had begun by that day, and factor 1 before
any such period; the factor-1 result is always reported as the baseline. It may project
dates before that configuration existed, marking the retrospective assumption; it never
backdates a configuration. A calibration may have been fitted on the compared days.
Manual measurements take priority over settlement injection and provisional
reported production under the current measurement-selection policy. Preserve
source snapshot IDs and provisional labels, and retain the same daylight,
missingness and native clipping rules. This diagnostic compares historical model
output with production; it is not forecast-accuracy evaluation or calibration
validation. The frontend opens this mode; the API's default archived replay
retains the preceding contract.

## Historical irradiance and measured performance

EPİAŞ `realtime-generation` is archived as provider-reported hourly production,
separate from Solcast weather estimates and user meter observations. The current
official interface labels `total` as MWh and identifies TEİAŞ as the source. This
is reported operating production, not a forecast or necessarily final settlement
energy. TEİAŞ describes its aggregate YTBS production statistics as gross, with
manual or automatic input and subsequent corrections. That aggregate definition
does not establish the meter boundary of an individual EPİAŞ plant series.

For a grid-delivery target, use settlement injection (`injection-quantity`, UEVM)
as the separately identified benchmark. Its definition is energy supplied to the
system during the settlement period. DUY articles 84–85 apply registered meter
configuration and applicable loss adjustments; UEVM is not automatically raw
connection-meter energy or injection minus all grid withdrawals (UEÇM). The
published data list specifies hourly values published in the following month
(A+1), not next-day availability. Resolve each service's plant ID by the same EIC;
the real-time and settlement IDs are different namespaces.

The authorized 2026-11-10 comparison for G4-BOR-1 GES, EIC `40W000005011621Y`,
used real-time ID `3154` and UEVM ID `5011621`. Both September acquisitions had
720 unique hourly records with no missing values. Real-time energy summed to
24,321.53 MWh versus 24,324.05 MWh UEVM: a signed difference of -2.52 MWh
(-0.01036%). Hourly MAE was 0.008694 MWh and the largest difference 0.64 MWh;
661 hours matched exactly, including 339 of 398 hours positive in either series.
Twelve hours disagreed on zero versus positive production. This supports a close
provisional proxy for this plant and month, not universal meter equivalence or
forecast accuracy. UEVM queries for 2026-10-31 and 2026-11-09 were empty at that
check; an empty response is not zero production or a fixed publication deadline.

Evidence strongly supports start labels: the inferred interval for EPİAŞ
`10:00+03:00` is `[10:00, 11:00)` Istanbul time. On 718 common September hours, same-label
real-time/UEVM MAE was 0.008719 MWh versus approximately 8.08 MWh with either
one-hour shift. Positive `06:00` production on September 15 also agrees with
the reference site's calculated 06:24 sunrise. In the November 9 TEİAŞ daily
report, hours are labelled 01:00–24:00; across 11 common nighttime hours, matching
EPİAŞ hour h to TEİAŞ h+1 gave MAE 475.81 MWh, versus 1,458.50 MWh for h and
2,659.58 MWh for h-1. Their different coverage prevents exact equality. This is
empirical evidence, not an explicit interval convention in the published API
schema; both the schema and CSV export only say "hour".

The accepted runtime contract is `hour-start-istanbul-v1`: preserve provider
labels and normalize them to explicit one-hour bounds. Collect operational and
settlement series separately with immutable revisions. Provisional real-time
production can supplement calibration as an explicitly identified proxy. Forecast-
performance metrics continue to use manual measurements and UEVM only.

Policy `manual-over-settlement-over-provisional-hour-v2` supplements manual
observations with valid UEVM hours, then provisional reported-production hours,
only for a `net_export` target. The plant configuration owns the measurement point;
EPİAŞ connection settings do not override it. Legacy connection declarations are
retained as migration-origin plant events, without changing archived acquisitions
or calibration eligibility. Select the latest available acquisition per day and
source kind for the current configuration and provider identity. Never fill
missing values from an older acquisition of the same source. A valid UEVM zero
overrides positive provisional production. This target includes settlement
adjustments; it does not assert raw connection-meter equivalence. Any overlapping
manual observation, including invalid/outage/curtailed readings, suppresses the
entire provider hour. Corrections append immutable observation revisions; explicit
revert withdraws the manual value and restores the latest eligible provider hour,
or missing data. Select the latest revision available at both the request's time
cutoff and observation ID watermark; a later replacement or revert never changes a
pinned fit. Current-head index metadata does not determine historical selection.
Corrections are absolute MWh values and retain precedence after provider refreshes;
the displayed provider original can change with its own immutable acquisition. Missing or invalid provider values remain missing, and the
existing daylight-coverage rules decide which days qualify. Do not split hourly
energy into invented half-hour measurements.

Record production snapshot IDs, source kind, provider identity and digests in the
dataset manifest. Flag provisional production independently from the statistical
assessment for short calibration windows. Existing runs retain
`manual-over-settlement-hour-v1` and their pinned settlement-only inputs. A stable
fingerprint of measurement values, quality, source identity,
configuration and matched reference content controls automatic refitting;
unchanged repeated acquisitions do not trigger a new fit. Preserve the request's
selected snapshot IDs separately for replay. Corrected inputs can prepare a new
candidate, including settlement replacing provisional production with identical
values. Completed fits and archived forecasts remain unchanged; new candidates
require manual activation or separately authorized guarded automatic promotion.

Solcast historical GHI/GTI are satellite/model-derived **estimated actuals**; they
are not automatically on-site measurements. AWS is a distribution channel, not a
statement about a dataset's measurement method, location coverage or usage rights.
Retain provider, product, location, interval, units, acquisition time and processing
version. Keep archived forecasts, estimated actuals and ground measurements distinct.

For weather-normalized plant performance, pair historical irradiance/weather with
the same intervals of plant meter/SCADA energy and known plant configuration.
GHI describes a horizontal plane; tilted or tracking arrays require appropriate
POA/GTI or DNI/DHI plus geometry, and a temperature model. A cumulative meter reading
must first become an interval energy delta. Identify whether energy is inverter AC
or net export and flag outages, curtailment and meter changes.

Historical irradiance without plant production cannot identify plant efficiency.
Production without independent irradiance cannot separate forecast bias, weather,
geometry, soiling, availability and degradation. The implemented multiplier corrects
combined reference-model transfer bias; it is **not** an estimate of panel degradation
or a calibration of true irradiance. Rooftop estimated-actual power is still a model
output. Historical GHI/weather file import and a physical performance model require
a verified dataset contract and are separate from this proxy calibration. Never use later estimated actuals as future forecast
features or as matched Solcast forecast GHI for reconstruction-equivalence claims.

## Primary references

- [EPİAŞ electricity API](https://seffaflik.epias.com.tr/electricity-service/technical/tr/index.html) and [reported-generation interface](https://seffaflik.epias.com.tr/electricity/electricity-generation/ex-post-generation/real-time-generation).
- [TEİAŞ YTBS data definitions](https://ytbsbilgi.teias.gov.tr/ytbsbilgi/frm_sorular.jsf) and [daily operating reports](https://ytbsbilgi.teias.gov.tr/ytbsbilgi/frm_istatistikler.jsf).
- [EPİAŞ published-data list, rows 104–105](https://www.epias.com.tr/wp-content/uploads/2022/01/S%CC%A7effaflik-Platformunda-Yayimlanacak-Veri-Listesi.pdf) and [DUY articles 81–85, official English translation](https://www.epias.com.tr/wp-content/uploads/2026/02/Electricity-Market-Balancing-and-Settlement-Regulation-29.12.2025.pdf).

- [Hobby contract](https://docs.solcast.com.au/docs/section/rooftop-sites-hobbyist).
- [Rooftop model](https://www.solcast.com/rooftop-pv-power-model).
- [Irradiance and transposition methodology](https://www.solcast.com/irradiance-data-methodology).
- [Historical time-series parameters and interval means](https://solcast.com/time-series).
- [PVWatts DC model inputs](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.pvsystem.pvwatts_dc.html) and [inverter clipping](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.inverter.pvwatts.html).
- [Huber regression](https://scikit-learn.org/stable/modules/linear_model.html#huber-regression) and [time-series evaluation](https://otexts.com/fpp3/tscv.html).
- [Astral sunrise/sunset definitions](https://sffjunkie.github.io/astral/package.html#astral.sun.sunrise) and [independent USNO MICA reference](https://griffithobservatory.lacity.gov/explore/observing-the-sky/whats-in-the-sky/the-sun/2025-sunrise-and-sunset/).
- [NREL weather-corrected performance ratio](https://research-hub.nlr.gov/en/publications/weather-corrected-performance-ratio/).
