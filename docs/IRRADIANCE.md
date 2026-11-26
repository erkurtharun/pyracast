# Irradiance contract

This page is the calculation contract: the quantities taken from the provider, their units and
time conventions, how irradiance and electrical energy are derived, how missing data and quality
flags are treated, and what calibration and evaluation do and do not claim. Which saved source a
screen selects, and how collection, calibration runs and activation are executed, is owned by the
flow pages under [flows/](flows/README.md) and linked where it matters.

## Target and claims

Pyracast reconstructs a horizontal irradiance **proxy** from Solcast Hobby rooftop PV power and
integrates it into a local day's incoming solar energy: interval GHI in W/m², daily energy in
kWh/m² and MJ/m². Incoming solar energy is not electrical generation; plant electricity is a
separate result in MW and MWh.

The reference for reconstruction is Solcast GHI from the same forecast issue, location, interval
and configuration. A satellite estimate or another provider's forecast does not establish
equivalence to Solcast, and no interval or daily accuracy is claimed without matched data.

## Provider inputs

- The reference site is horizontal by contract: a binding stores no tilt and the baseline refuses
  any other. A binding records `capacity_kw` (the verified AC normalization capacity),
  `dc_capacity_kw` (module nameplate, null when not declared), `power_basis`, azimuth,
  `losses_percent` (0 to below 100), installation date, coordinates and IANA timezone. Do not
  assume 1 kW means 1 m² or that all losses can be disabled.
- Hobby endpoints: `/rooftop_sites/{resource_id}/forecasts` and
  `/rooftop_sites/{resource_id}/estimated_actuals`. The public contract documents 30-minute
  intervals, up to 10 requests per UTC day per account (the account quota is capped at 10),
  forecasts through 14 days and estimated actuals for seven days.
- The adapter accepts only `PT30M` rows, reads `pv_estimate` (kW, nullable, finite) and turns
  `period_end` into explicit UTC bounds `[end - 30 min, end)`. Quantile columns are not used, the
  central value is not assumed to be P50 or an expectation, and the Hobby response gives no issue
  time, so `issued_at` stays null; it is never invented. The raw payload is retained whole; the
  normalized intervals are clipped to the requested window, for a forecast the three local dates
  `[today, today + 3)`.
- Forecast requests ask for the smallest whole-hour horizon reaching the end of the day after
  tomorrow; estimated-actual requests ask for the shortest whole-hour lookback reaching the oldest
  gap, capped at 168 hours. A gap is a completed native half-hour of those 168 hours that overlaps
  the local whole hours around daylight (sunrise rounded down, sunset up) and that no archived
  native estimated actuals cover; a polar night has none. Scheduling, quota reservation and
  archiving are in
  [forecast-collection-scheduling](flows/forecast-collection-scheduling.md) and
  [forecast-collection-execution](flows/forecast-collection-execution.md).

## Irradiance baseline

`power-capacity-proxy-v1` estimates each native interval as

`ghi_wm2 = 1000 * power_kw / capacity_kw`

marked `unvalidated`. Power and capacity must share one explicit AC/DC basis and the capacity
semantics must be verified, otherwise estimation is refused. For a 1000 kW AC reference, 600 kW
of interval-mean power gives 600 W/m²; a half-hour then contributes 0.3 kWh/m². The baseline is
not a physical identity: Solcast's rooftop model is empirical and includes air temperature and
dynamic snow losses, which a 0% loss setting does not disable.

Each interval carries a sunlight class (`day`, `night`, `partial`, `unknown`). GHI stays missing,
with a flag, for missing (`missing_power`), nonfinite (`nonfinite_power`) or negative
(`negative_power`) power, for zero power outside a declared night (`ambiguous_zero_power`), for
positive power at night (`night_power_conflict`) and for a nonfinite result. `partial` and
`unknown` add `partial_sunlight` or `sunlight_unknown`. Live Solcast intervals carry `unknown`, so
their zero intervals, nights included, never establish zero GHI; only the fixture provider
declares day and night.

Limits that hold for every estimator:

- `P90-P10` is forecast uncertainty, not within-period variance; no Jensen correction is derived
  from it.
- No assumed IAM divisor or universal solar-angle cutoff. An invalid low-sun inversion is not zero
  irradiance.
- External GHI is never substituted into the reconstructed result.
- Tilted-roof POA and DNI/DHI recovery are not implemented. A two-array isotropic inversion would
  not be Solcast's documented Reindl inverse.

## Daily energy

The local day runs from local midnight to the next local midnight in the binding's IANA timezone,
converted to UTC, so DST days last 23 or 25 hours. For interval-mean irradiance:

`energy_kwh_m2 = sum(ghi_wm2 * overlap_seconds) / 3_600_000`

`energy_mj_m2 = energy_kwh_m2 * 3.6`

A partial overlap assumes the interval mean holds over the overlap and flags
`prorated_interval_mean`. Overlapping or duplicate intervals are refused. Coverage is the union of
overlaps; 48 intervals are not assumed and missing values are never turned into zero.

The result reports `partial_energy_*`, `coverage_seconds`, `valid_coverage_seconds`,
`coverage_fraction` and `complete`. The full-day total is non-null only when valid values cover the
entire local day, night included; otherwise it is null with `incomplete_day`, and the partial
energy remains. Interval P10/P90 sums are not daily quantiles. The payload also carries region,
date, timezone, surface, day bounds, intervals with flags, the vintage (provider, source kind,
collection and issue time, binding revision), the binding configuration and `model_version`.

### Daylight summary

Forecast and estimated-actual results also carry a `daylight` summary under
`astral-3.2-sea-level-v1` with `energy_scope: whole_native_intervals`. Its coverage counts valid
values inside astronomical daylight. Its energy keeps every whole native source interval that
overlaps daylight, clipped only to the local day, so sunrise and sunset never split an interval
mean into invented day and night energy; original native bounds decide this even after
composition or outage splits. The total is non-null only when valid values cover daylight and
every selected boundary interval, so a missing boundary fragment leaves it null even at 100%
daylight coverage. Polar night has no window, no total and a null `coverage_fraction`; a polar
day's window is the whole local day, so its total needs valid values for all of it. The summary
does not depend on the 30/60-minute display resolution.

On irradiance results, `input_coverage_seconds` and `input_coverage_fraction` report daylight
covered by finite, nonnegative source power, valid zeroes included, measured on the selected
interval pieces; `coverage_fraction` still reports usable derived GHI. Complete input coverage
therefore does not establish a complete irradiance total. Electrical and outage summaries carry no
input coverage.

### Astronomical daylight

`astral-3.2-sea-level-v1` uses the apparent upper solar limb, standard refraction and a sea-level
horizon, without terrain or shading. Astral searches events per UTC date and can miss one near
00:00 UTC, so the horizon test is also sampled every 30 minutes from 30 minutes before to
30 minutes after the local day. Where two neighbouring samples agree but the sun could reach the
horizon and return between them (at most 16° of altitude per hour), the span is halved down to
one second. An Astral event of the same direction is kept when it lies within 60 s of a bracket
and the horizon test changes state within 60 s around it; otherwise the crossing is bisected to
the millisecond, and unclaimed Astral events are dropped. The day keeps events in `[start, end)`.
Any valid coordinate and timezone yields windows; only a local date the timezone skips is
refused. An interval ending exactly at sunrise or starting at sunset has no daylight overlap.


## Offline conditional reconstruction

`scripts/calibrate_irradiance.py` is a research tool: no product path reads its output. It pairs a
Historical Rooftop PV `PT30M` power export (`pv_power_rooftop`) with six contiguous `PT5M` weather
rows (`ghi`, `air_temp`, `snow_soiling_rooftop` in percent) on explicit UTC bounds; unmatched
intervals are counted, never filled. The tilted `gti` field is not the horizontal reference. The
operator establishes that both exports share coordinates, horizontal geometry, capacity and loss
settings. Inputs, failures and outputs are in
[offline-irradiance-calibration](flows/offline-irradiance-calibration.md).

The forward model is `F(g,t) = c0 + c1*g + c2*t + c3*g*t + c4*g² + c5*t²` with `g = GHI/1000`,
`t = (air_temp_c - 25)/25`, capacity `C` in kW and snow transmission `q = 1 - snow_loss_fraction`.
It fits `P/C` against the native five-minute means of `q*[1, g, t, g*t, g², t²]` by scaled QR with
reorthogonalization and rank checks, on positive-power pairs whose six native GHI values exceed
25 W/m² and whose transmissions are at least 0.05. These are engineering support guards, not
physical thresholds or confidence levels.

`quadratic-weather-inverse-v1` applies it with half-hour mean weather: `a = c4`,
`b = c1 + c3*t`, `d = c0 + c2*t + c5*t²`, `y = P/(C*q)` and the ascending root

`GHI = 1000 * 2*(y - d) / (b + sqrt(b² - 4*a*(d - y)))`

which also covers `a = 0` without cancellation. The model must be increasing over the support
`[0, 1400 W/m²]` (an implementation domain, not a physical bound), the root must be finite and
inside it and the forward residual small; the sensitivity is `dGHI/dP = 1000/(C*q*(b + 2*a*g))`.
Missing or invalid weather, opaque snow, transmission below 0.05, zero power and unsupported roots
give no estimate; clipping is never inferred from power alone. Temperatures outside the fitted
range are flagged `temperature_extrapolation`.

Five-minute variance and covariance are lost in a half-hour mean, so this is a **conditional point
approximation**, not an exact inverse or an irradiance bound. Native GHI fits and scores the model
but is never an inverse input. Temperature and snow must be known at acquisition or issue time;
without them the model is unavailable and the live baseline stays the unvalidated
power/capacity proxy.

The script trains on the first 60% of complete local days, validates on the next 20% and tests on
the rest, never refitting on validation or test. Its daily metric is partial energy over the same
identifiable intervals for both methods; it validates neither full-day totals nor forecasts.
Inputs must lie inside the working directory:

```text
uv run --locked python scripts/calibrate_irradiance.py --power POWER.json --weather WEATHER.json --capacity-kw 1000 --timezone Europe/Istanbul --output OUTPUT_DIR
```

On the authorized horizontal reference case (17,376 paired half-hours, 361 complete local days:
216 training, 72 validation, 73 test), 1,713 identifiable test intervals gave GHI MAE 1.3002 W/m²
against the baseline's 50.4214 W/m² and RMSE 6.7675 W/m²; daily partial-energy MAE was 0.007262
against 0.589119 kWh/m². The test set had no snow-covered intervals and 277 flagged temperature
extrapolations; the largest daily partial-energy error was +0.175536 kWh/m². This is historical
model-output agreement at one site, not measured irradiance accuracy, seasonal validity or
forecast equivalence.

## Plant electrical generation

### Reference power transfer

`reference-power-transfer-v1` scales normalized reference power directly:

`raw_power_mw = power_kw / capacity_kw * plant_dc_capacity_mw`

`plant_power_mw = min(factor * raw_power_mw, applicable_limit_mw)`

The factor is 1 until a calibration is active. The transfer is flagged
`unvalidated_reference_transfer`; it is not a PVWatts model and does not claim reference AC
capacity equals plant DC capacity. Plant temperature and individual losses are not inferred from
the reference site. Mounting (unknown, fixed or tracking) with optional fixed-array tilt and
azimuth clockwise from true north is declared metadata; only tracking is consumed (below).
Results without tracking geometry are flagged `geometry_not_modeled`. Plant geometry is never
inferred from a weather filename, and a grid export limit is not an inverter rating.

The applicable limit is the known inverter capacity, and for a `net_export` measurement basis the
smaller of inverter and export limit; unknown limits are omitted, not invented. Clipping applies at
the native source interval **before** hourly aggregation: 80 and 120 MW over two half-hours with a
100 MW limit give 90 MWh for the hour. Interval means cannot recover clipping at finer resolution.
Power is integrated over UTC overlap durations at 30- or 60-minute resolution; missing power gives
a null total with separate partial energy and coverage. Electrical zero is valid even where it
cannot establish zero GHI, and plant power is never derived by filling ambiguous GHI.

### Tracking geometry transfer

One number cannot scale a horizontal reference to a tracking plant: the plane-of-array gain
depends on solar position and beam share. A `tracking` configuration whose binding declares DC
capacity multiplies each native reference interval by a gain before calibration and clipping:

`raw_power_mw = power_kw / capacity_kw * plant_dc_capacity_mw * gain`

At the interval midpoint, with Astral's sea-level solar position:

1. GHI from the loss-free fraction `p = power_kw / dc_capacity_kw / (1 - losses_percent/100)`
   by inverting `p = 0.9964*g - 0.1484*g²` (`g = GHI/1000`, fitted on the authorized horizontal
   case and used only for the clearness index).
2. Erbs diffuse fraction, with the clearness index capped at 1.
3. A north–south horizontal axis with ground coverage ratio 0.40, a ±55° limit and slope-free
   backtracking.
4. Hay–Davies transposition with a 1361 W/m² solar constant and albedo 0.20.
5. Gain = plane irradiance / GHI, capped at 6, times `1 - 0.0035/29 * excess` for warmer tracker
   cells (−0.35 %/K at 29 W/m²K), with the plane excess over GHI clamped to [−200, 600] W/m².

Zero reference power, a zenith of 87° or more, or GHI of at most 1 W/m² give gain 1. Results are
flagged `assumed_tracker_geometry`: the declared axis, row spacing, bifacial gain and terrain are
not modeled.

`tracking-geometry-transfer-v1` gives the refraction-adjusted zenith to both the Erbs split and
the geometry. `tracking-geometry-transfer-v2` gives the true zenith to the Erbs split (clearness
index, DNI and the 87° cut-off) and the apparent zenith to rotation and transposition, as
[STACK.md](STACK.md) requires; the two differ by up to about 5% in the gain near the cut-off and
below 0.1% at high sun. `tracking.DEFAULT_MODEL` (v1) names the version of new results.
Every result records its model (`model_version` on forecasts, issuances and recomputed performance
days, `transfer_model` on calibrations) and is replayed under it; an unknown model is refused. A
tracking plant forecasts with the version its active calibration was fitted under and with the
default while uncalibrated; a refit uses the default. A calibration applies only within the
transfer family it was fitted under (direct or tracking); one without `transfer_model` is direct.
Production diagnostics are shown only next to a forecast of the model they were computed under.

`tests/fixtures/tracking_gain_reference.json` (v1) and `tracking_gain_reference_v2.json` (v2)
hold gains from an independent pvlib 0.16.1 chain: the same GHI estimate, `erbs`, `singleaxis`
(axis azimuth 180°, ±55°, backtracking, GCR 0.4), Hay–Davies `get_total_irradiance` with pvlib's
extraterrestrial irradiance (solar constant 1366.1 W/m²) and albedo 0.20, and the `pvsyst_cell`
temperature factor at 29 W/m²K, rounded to four decimals. `scripts/check_scientific.py` recomputes
both chains in CI on Windows and Ubuntu and requires the stored gains within 0.0001 and the
pure-Python gain within 0.012; the unit tests hold the model to the stored gains within 0.012.
`tests/fixtures/tracking_v1_golden.json` (400 gains, 192 interval gains, 25 reference powers,
including night, twilight and the 87° edge) holds v1 within a relative 1e-12 on every platform,
not bit for bit, because platform maths libraries differ in the last bits; stored results are never
recomputed in place.

`scripts/evaluate_tracker_geometry.py` is the decision rule for the default. Read-only on an
archive database, printing aggregates per plant ordinal only, it compares hourly EPİAŞ readings of
the plant's enabled connection (settlement before real-time, latest snapshot per day and kind)
with estimated actuals inside daylight on complete days, skipping hours a manual observation
overlaps. Each week after the first is scored with factors that `fit_calibration` refits per
version on the 37 preceding days. v2 counts as not worse unless the 95% delete-one-week jackknife
interval of RMSE(v2) − RMSE(v1) lies wholly above zero, with at least four scored weeks.

Evidence on the authorized case (2025-11-05 to 2026-11-03, hourly settlement energy, weekly
rolling origin refitting on the trailing 30/60/90 days and scoring the next seven): direct
transfer gave interval MAE 9.39/9.68/9.71 MW, daily MAE 79.4/82.6/86.0 MWh and factors from 0.99
to 1.73; with the gain 6.97/6.78/6.40 MW, 61.3/59.2/55.1 MWh and factors from 0.83 to 1.00.
Separate morning, midday and afternoon factors were 1.48/0.95/1.34 on direct transfer and
0.89/0.89/0.88 with the gain, where they no longer improved unseen days; Solcast DNI/DHI instead of
the reference-derived split changed in-sample MAE by 0.2 MW. One scalar therefore stays the
calibrated quantity. This is transfer accuracy against estimated actuals, not day-ahead accuracy.

Known limit: the rooftop reference includes dynamic snow soiling. In the case year 57 days had
reference power near zero while the plant produced 280–530 MWh; the gain cannot restore that
energy, so such days stay large underestimates.

### Composition and outages

`latest-forecast-coverage-v2` composes forecasts: the newest acquisition owns its coverage
window, internal missing values included, and older acquisitions fill only the remaining windows.
Native intervals split at ownership boundaries before calibration, clipping and aggregation keep
their mean and original bounds and flag `uniform_mean_proration`; every contributor is retained
and the same pieces feed the normal and outage scenarios. Estimated actuals compose the same way
under `native-estimated-actuals-coverage-v1`; nulls and gaps inside newer coverage never recover
older values, and native, capacity-scaled and imported products are not mixed. Today's automatic
view uses `estimated-actuals-then-forecast-v1`: valid actuals whose original native end has
elapsed replace forecasts (`mixed` source), and actuals alone once they cover daylight and its
whole native boundary intervals. These actuals are model estimates, not metered production. Which
source each screen selects is in [forecast-view](flows/forecast-view.md) and
[irradiance-forecast-view](flows/irradiance-forecast-view.md).

The outage scenario (`reported-operating-limits-v2`) splits native intervals at reported
boundaries, scales the uncapped transfer by remaining DC over nameplate DC (at most 1), applies the
factor, then the smaller of the normal limit, the reported inverter cap and, for `net_export`
only, the reported export cap. Disconnected modules belong in remaining DC; lowering only the
inverter cap does not model their lost collection area. A limit reporting no capacity, or a
configuration whose DC, inverter, export capacity or measurement basis differs from the one the
outage was declared under, makes the affected power missing (`unknown_outage_impact`,
`outage_configuration_changed`). Zero availability gives zero power even when reference power is
missing. This is a conditional availability approximation, not a component-level simulation. See
[outage-management](flows/outage-management.md).

## Production calibration

Calibration fits one positive multiplier of the reference transfer against metered interval
energy. The flow is in [calibration-run](flows/calibration-run.md); activation and automatic
promotion are in [calibration-activation](flows/calibration-activation.md) and
[calibration-automation](flows/calibration-automation.md).

### Dataset

- Policy `regularized-huber-v7` uses completed local days whose whole meter intervals overlapping
  astronomical daylight are `valid` and cover every daylight window
  (`whole-meter-intervals-cover-daylight-v1`); boundary intervals keep their full energy, reference
  data must cover each selected interval, and polar night yields no day. Night readings are
  optional and never synthesized as zeros. Days with daylight gaps, invalid readings, outages or
  declared curtailment fall out; low output alone never implies an outage.
- An incident overlapping any part of a meter interval excludes that interval
  (`exclude-whole-overlapping-meter-interval-v1`), so the coverage rule can exclude the day.
  Incident revision IDs are pinned; a later correction keeps the fit's evidence but blocks its
  activation. Incident effects are never folded into the multiplier.
- The reference is Solcast estimated actuals, never archived forecasts. The matching pool holds at
  most 60 calendar days ending on the latest recorded completed day; training uses the valid days
  within the 30 calendar days before the final holdout (`recent-calendar-days-v1`). Each day uses
  one reference source. Pins, cutoffs and watermarks are in
  [calibration-run](flows/calibration-run.md).
- Measurements follow the plant measurement policy (see
  [Measured production](#measured-production)); periods with different capacities or meter
  definitions are not mixed. Declared configurations applied to older readings are an explicit
  retrospective assumption. External historical weather files are not used.

### Fit

Duration-weighted Huber loss on power residuals normalized by DC capacity, delta 0.05, an identity
penalty `0.001 * (factor - 1)²` and the factor domain [0.25, 2]. Native clipping stays inside the
objective; the one-dimensional solver evaluates every clipping breakpoint and each segment and does
not assume the capped objective is convex. These are versioned engineering defaults.

Training excludes meter intervals at or above 98% of the known cap, intervals without positive
reference power and days without an interval of both positive measured energy and positive
reference power; `zero_production_training_days` counts the training days without positive
measured energy. Zero-source samples do not identify a multiplicative factor.
Clipped and zero days stay in the holdout and its scores. Training support needs five days, each
with at least two hours of positive native power not already clipped at the minimum factor and at
least 0.02 hours of normalized slope-squared exposure (the sum of meter-interval duration times
the squared ratio of mean uncapped power to DC capacity, capped at one; two hours at 10% of DC
capacity reach it). These guards prevent a fit on one useful measurement; they are not confidence
levels.

### Split and acceptance

- Fewer than seven complete days: `insufficient`, with diagnostics and no factor. 7–29 days hold
  out the last max(2, ceil(20% of days)) and give a `provisional` assessment; 30 or more hold out
  the last seven and are `evaluated`. The holdout is never refitted. Intervals within a day are
  correlated, so hundreds of half-hours are not hundreds of independent weather realizations.
- A Huber candidate is `ready` when holdout interval MAE and daily MAE both improve by more than
  1% over factor 1, the factor lies more than 1e-6 inside the bounds, neither MAE worsens against
  the pinned incumbent, and absolute power and daily-energy bias worsen against neither; "worsens"
  allows a tolerance of 1e-9 × max(1, |reference|).
- If those guards fail and the incumbent differs from 1, `identity_recovery` (factor 1) is
  accepted when it improves both MAEs by at least 1% over the incumbent without worsening either
  absolute bias. The fitted Huber factor and metrics are kept and `candidate_kind` names the
  selection. It never resets a factor because time has passed.
- Any holdout day with zero measured energy and positive reference power rejects either kind with
  `zero_production_holdout`, counted in `zero_production_holdout_days`; a day with zero source and
  zero production does not.
- With at least 30 days, up to three expanding-training folds with three-day evaluation inside
  the training window diagnose transfer stability; they never include the holdout or tune
  anything. Source-positive interval MAE is reported separately over meter intervals with positive
  reference power.
- Reports give interval power MAE and bias (MW), daily daylight-interval energy MAE and bias (MWh,
  not 24-hour totals), included and excluded days, censored counts, the chronological split,
  `metric_scope` and the daylight policy. `uncertainty: not_estimated`: the 1% margin is an
  operational rule, not a significance test, and no probability bands are exposed.

Window evidence on the authorized case with the tracking transfer, weekly origins on the same
evaluation days: interval MAE 5.58, 5.60, 5.58 and 5.62 MW for 30-, 45-, 60- and 90-day windows,
5.88 MW for 14 days and 5.75 MW for 180; the 30-day window had the lowest daily MAE (46.2 MWh).
The monthly mean of the 30-day factor moved between 0.86 and 0.94 through the year, so one recent
month follows that drift without a window search. A factor applies to its training period
onward.

### Automatic promotion

Promotion needs, besides a `ready` candidate and an unchanged activation context: an `evaluated`
fit with at least 30 valid days and exactly seven holdout days before today; the latest holdout
day at most seven and the earliest at most fourteen days old; both MAEs at least 1% below the
pinned incumbent; and source validation `native-final-7-settings-matched-selected-30-v3` — all
seven holdout days from the current binding's native Hobby estimated actuals or authorized
capacity-scaled copies of them, and at least 30 training and holdout days that are such days or
the settings-matched historical export, recomputed from the per-day source manifest. Only against
an incumbent calibration does it also need seven holdout dates after the incumbent's last holdout
date (or window end) and no `suspected` production diagnostic for a Huber candidate (identity
recovery to 1 may pass one); an uncalibrated plant is held to neither. These are freshness and evidence guards, not statistical confidence
limits. Holdout improvement against estimated actuals does not establish better future forecasts.

### Reference sources

- `native-thirty-days-else-settings-matched-pool-v2` selects the cohort: native Hobby days alone
  once 30 complete matched days exist; below that, native days pooled with an authorized
  historical rooftop export of the same configuration; days whose source product cannot be
  established, imports whose source settings did not match included (`unknown`), only as a
  separate cohort, never pooled. Pooled products, coverage counts and excluded samples are
  recorded. Import rules are in [historical-reference-import](flows/historical-reference-import.md).
- For a past day's estimated actuals, native acquisitions keep precedence once they cover daylight
  and its whole boundary intervals, even with nights missing; otherwise a complete authorized
  capacity-scaled copy, then a historical export, takes the day. Normalization creates no observed
  days or provider requests.
- `normalize_reference_capacity` copies acquisitions after a confirmed capacity-only change with
  `P_new = P_old * C_new / C_old`. Both bindings must declare DC capacity with equal DC/AC ratios
  (an undeclared value leaves the provider's implicit ratio in effect), all other model inputs
  must match, the capacities must differ, and imported exports and earlier copies are never
  copied. Originals stay unchanged; copies keep acquisition and issue times, become available at
  conversion time, and forecast copies get results recalculated at the target capacity. Earlier
  forecasts, calibrations and measurements are not rewritten. See
  [reference-capacity-normalization](flows/reference-capacity-normalization.md).

Evidence for the DC rule: a reference site with undeclared DC capacity returned 1.25 times the
power of a site declaring equal AC and DC capacity on 116 common daylight intervals, so its scaled
copies were not equivalent.

## Production diagnostics

Every analysis, insufficient or rejected included, reports `strong-unclipped-power-diagnostics-v2`.
Its reference is the pinned active calibration (`reference_kind: active_calibration`) or factor 1
(`uncalibrated_reference`); the new candidate is never its own reference and neither reference
establishes a healthy plant. The window spans ten calendar days ending on the latest complete
matched day, observed zero-production days included. It keeps meter intervals whose predicted mean
power is at least 25% of DC capacity and where neither prediction nor measurement reaches 98% of
the cap; a day qualifies with at least three such hours.

Each qualifying day reports its measured/predicted ratio, hours, `shortfall` (below 0.92) or
`within_threshold`, and whether it observed zero production. With at least three qualifying days
the median ratio gives `suspected` below 0.92 and `normal` otherwise; fewer give `insufficient`.
The latest matched day's status and ratio are reported separately; without enough hours it is
`insufficient`, not evidence of recovery. The median is not total production loss, a fault
diagnosis or a capacity estimate; the thresholds are engineering guards.

A plant outlook carries it as `production_diagnostic` only for a `forecast` result without
a frozen scenario, on a day not yet ended, when the run's configuration, production
identity, incidents, factor and transfer model still apply and its latest matched day is before
today; it is `stale` when that day is more than two days before today. The diagnostic changes
neither forecast power nor any capacity; only reported operating limits drive the outage scenario.

## Forecast evaluation

The performance endpoint ([forecast-performance](flows/forecast-performance.md)) scores 1 to 90
completed local days within the past year. The forecast screen's past-day forecast, chosen by
closeness to production (`closest_to_production`, [forecast-view](flows/forecast-view.md)), is
never scored; Forecast accuracy keeps its own selection below.

- `archived_forecast` scores the immutable issuance published before the target day's first
  astronomical daylight (`latest-published-pre-daylight-issuance-v1`; polar day, polar night and
  midnight-crossing daylight use local midnight). Acquisition and publication precede that cutoff
  and a known provider issue time does not follow acquisition, so a pre-dawn collection can
  replace the previous evening's forecast. An optional `issuance_cutoff` (an offset-free local
  time on the previous day) restricts selection further; the latest eligible issuance wins even
  when partial. Its frozen calibrated and factor-1 native vectors, original clipping included, are
  scored without recomputation, against manual measurements over settlement injection
  (`manual-over-settlement-hour-v1`). A day whose issuance has another measurement basis or
  timezone is `configuration_not_comparable`. Reported incidents do not remove valid production;
  invalid or outage readings stay excluded.
- `historical_model` applies the current configuration to saved estimated actuals, with the newest
  activated calibration whose training period had begun by that day and factor 1 before; it may
  have been fitted on the compared days. It uses `manual-over-settlement-over-provisional-hour-v2`
  and flags provisional production. It compares model output with production; it is neither
  forecast accuracy nor calibration validation.
- `daylight_only=true` (what the frontend sends) requires complete daylight coverage and reports
  `metric_scope: daylight_intervals`; otherwise the whole local day is required. Whole meter
  boundary intervals are kept, valid zero-production and zero-source days stay scored, missing
  days never become zero errors, and source-positive MAE is null without such intervals. Results
  give daily measured, predicted and factor-1 MWh, signed error, identities, cutoffs, excluded-day
  reasons, aggregate MAE and bias, relative error and a skill score against factor 1. Nothing is
  trained or activated, and no confidence intervals are produced.

## Measured production

EPİAŞ `realtime-generation` is archived as provider-reported hourly production (`total` in MWh,
TEİAŞ as source): reported operating production, not a forecast or final settlement energy. For a
grid-delivery target, settlement injection (`injection-quantity`, UEVM) is the benchmark: energy
supplied to the system in the settlement period after registered meter configuration and loss
adjustments (DUY articles 84–85), not raw connection-meter energy or injection minus withdrawals
(UEÇM). It is published hourly in the following month (A+1); an empty response is not zero
production. Each service's plant ID is resolved by the same EIC; the two IDs are different
namespaces. Collection is in [epias-collection](flows/epias-collection.md).

`hour-start-istanbul-v1` reads a label `10:00+03:00` as `[10:00, 11:00)` Istanbul time. Evidence:
on 718 common September hours of one plant, same-label real-time/UEVM MAE was 0.008719 MWh versus
about 8.08 MWh with either one-hour shift; real-time and UEVM energy differed by −0.01% over the
month. The published schema only says "hour"; this is empirical, and supports a close provisional
proxy for that plant and month, not universal meter equivalence.

`manual-over-settlement-over-provisional-hour-v2` supplements manual observations with valid UEVM
hours, then provisional real-time hours, only for a `net_export` target. Any overlapping manual
observation, invalid or outage readings included, suppresses the whole provider hour; a valid UEVM
zero overrides positive provisional production; missing provider values are not filled from an
older acquisition; hourly energy is never split into half-hours. Manual corrections are immutable
revisions selected at the request's time cutoff and ID watermark. See
[production-history](flows/production-history.md).

Solcast historical GHI/GTI and rooftop power are model-derived **estimated actuals**, not on-site
measurements. Keep archived forecasts, estimated actuals and ground measurements distinct, and
never use later estimated actuals as forecast features or as matched Solcast forecast labels.
Weather-normalized performance needs the same intervals of plant energy, POA or DNI/DHI with
geometry and a temperature model; production without independent irradiance cannot separate
forecast bias, weather, geometry, soiling, availability and degradation. The multiplier corrects
combined reference-transfer bias; it is **not** a degradation estimate or an irradiance calibration.

## Primary references

- [Hobby contract](https://docs.solcast.com.au/docs/section/rooftop-sites-hobbyist), [rooftop model](https://www.solcast.com/rooftop-pv-power-model), [irradiance and transposition methodology](https://www.solcast.com/irradiance-data-methodology) and [historical time-series parameters and interval means](https://solcast.com/time-series).
- [EPİAŞ electricity API](https://seffaflik.epias.com.tr/electricity-service/technical/tr/index.html) and [reported-generation interface](https://seffaflik.epias.com.tr/electricity/electricity-generation/ex-post-generation/real-time-generation).
- [TEİAŞ YTBS data definitions](https://ytbsbilgi.teias.gov.tr/ytbsbilgi/frm_sorular.jsf) and [daily operating reports](https://ytbsbilgi.teias.gov.tr/ytbsbilgi/frm_istatistikler.jsf).
- [EPİAŞ published-data list, rows 104–105](https://www.epias.com.tr/wp-content/uploads/2022/01/S%CC%A7effaflik-Platformunda-Yayimlanacak-Veri-Listesi.pdf) and [DUY articles 81–85, official English translation](https://www.epias.com.tr/wp-content/uploads/2026/02/Electricity-Market-Balancing-and-Settlement-Regulation-29.12.2025.pdf).
- [PVWatts DC model inputs](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.pvsystem.pvwatts_dc.html) and [inverter clipping](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.inverter.pvwatts.html).
- [Huber regression](https://scikit-learn.org/stable/modules/linear_model.html#huber-regression) and [time-series evaluation](https://otexts.com/fpp3/tscv.html).
- [IEA Wind forecasting evaluation guidance](https://www.iea-wind.org/wp-content/uploads/2022/06/IEAWind_Task36_Recommended_Practice_Part2_2nd_Edition_public.pdf) and [IEA PVPS solar-resource handbook](https://iea-pvps.org/wp-content/uploads/2024/09/Best-Practices-Handbook-for-the-Collection-and-Use-of-Solar-Resource-Data-for-Solar-Energy-Applications-Fourth-Edition-.pdf).
- [Astral sunrise/sunset definitions](https://sffjunkie.github.io/astral/package.html#astral.sun.sunrise) and [independent USNO MICA reference](https://griffithobservatory.lacity.gov/explore/observing-the-sky/whats-in-the-sky/the-sun/2025-sunrise-and-sunset/).
- [NREL weather-corrected performance ratio](https://research-hub.nlr.gov/en/publications/weather-corrected-performance-ratio/).
