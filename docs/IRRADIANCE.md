# Irradiance contract

## Target

Reconstruct **Solcast's implied horizontal irradiance** from Hobby PV forecasts and integrate it into the next local day's incoming solar energy. Output interval GHI in W/m² and daily energy in kWh/m², optionally MJ/m². Incoming solar energy is different from electrical generation.

The reference for reconstruction is Solcast GHI from the same forecast issue, location, interval and configuration. A satellite estimate or another provider's forecast does not establish equivalence to Solcast. Later physical measurements support a separate forecast-accuracy evaluation.

## Provider inputs

- Start with one authorized horizontal reference site per region: `tilt=0`. Record AC/DC capacity, azimuth, losses, installation date, coordinates, timezone and available snow/shading behavior. Confirm the saved settings and actual response schema. Do not assume 1 kW means 1 m² or that all losses can be disabled.
- Hobby endpoints: `/rooftop_sites/{resource_id}/forecasts` and `/rooftop_sites/{resource_id}/estimated_actuals`. The current public contract documents 30-minute intervals, up to 10 requests per UTC day per account, two nearby arrays, forecasts through 14 days and estimated actuals for seven days. Represent entitlement explicitly and verify it for each authorized account.
- Archive each acquired forecast vintage. Estimated actuals are not archived forecasts. Retain raw payload, collection time, provider issue time when available, interval bounds, units and configuration revision. Never invent an unavailable issue time.
- Normalize source-specific units and timestamp conventions at the adapter boundary. Confirm central/quantile semantics rather than assuming the central value is P50 or an expectation.

## Estimation

Implement a replaceable estimator with typed inputs and outputs containing values, method/version and quality flags.

1. Start with `G_proxy = 1000 * P_kw / capacity_kw`, explicitly marked **unvalidated** and using verified capacity semantics. It is a baseline, not a physical identity.
2. If the actual Solcast forward model and its inputs are available, invert it only on a unique, well-conditioned domain and check the forward residual.
3. Otherwise compare a small regularized/appropriately monotone calibration against the baseline using matched Solcast labels. Fit offline and deploy versioned artifacts; do not train inside requests or automatically replace a model after every refit.

Solcast publicly describes an empirical AC model involving irradiance and air temperature. A PVWatts/NOCT/SAPM surrogate is not its verified inverse. Unknown losses, clipping and time aggregation can prevent exact recovery. Temperature or wind features must be available at forecast issuance and improve held-out results before adoption.

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

## Validation and output

Primary metrics are daily kWh/m² MAE and signed bias against matched Solcast forecasts. Also measure daylight GHI error, low-sun behavior and coverage. Use chronological day blocks and held-out locations for geographic claims. Avoid relative errors near zero and leakage from later observations. A few weeks can verify the pipeline; they do not establish seasonal validity.

Return region/date/timezone, surface, interval bounds and GHI, daily/partial energy, completeness, source vintage, collection time, binding/model revision and quality flags. Tests cover unit conversion, local midnight/DST, missingness, overlap, solver validity and selection of information available at issuance. Keep the pure calculation core free of network/database access.

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

For inverter AC production, apply the known inverter limit. For net export, apply
the smaller known inverter/export limit. Omit unknown limits rather than inventing
them. Thus 130 MW DC and a 100 MW export limit are separate inputs. Apply clipping
at the native source interval **before** hourly aggregation: native powers 80 and
120 MW over two half-hours with a 100 MW limit produce 90 MWh for the hour.
Interval means cannot recover clipping at finer time resolution.

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

Policy `regularized-huber-v3` uses completed local days with complete, matched
astronomical daylight coverage and `valid` observations. Select whole meter
intervals with positive overlap with daylight; keep sunrise/sunset boundary slots
and their full energy without prorating. Valid measurements must cover every
daylight window, and reference data must cover each entire selected meter interval.
Exclude days containing daylight gaps, invalid records, outages or declared curtailment.
Night readings are optional and excluded from this scope, never synthesized as zeros.
Do not infer outages from low output alone. Exclude measurements at or above 98% of the known cap from
training because clipping hides uncapped performance; retain valid clipped intervals
in evaluation. Zero-source samples do not identify a multiplicative factor.
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

At least seven complete matched days are required. For 7–29 days, reserve the last
max(2, ceil(20% of days)) for evaluation; for 30 or more, reserve the last seven.
Fit only earlier days and do not refit on the holdout. Seven days can check the
pipeline and support a **provisional** fit; a month covers more conditions but
neither duration proves seasonal validity. Intervals within a day are correlated,
so hundreds of half-hours are not hundreds of independent weather realizations.
Collect up to 90 production days initially, while matched Solcast estimated-actual
history grows through daily retrieval. Ninety matched days can cover more operating
conditions, but do not guarantee a better fit; retain chronological validation and
the same acceptance criteria. External historical weather files are not used by
this reference-power calibration.

Report interval power MAE/bias (MW), per-day daylight-interval energy MAE/bias (MWh),
included/excluded days, censored counts and the exact chronological split. These
energy sums are not 24-hour totals. Preserve `metric_scope`, astronomical policy
and UTC daylight windows in the dataset manifest; compare all factors on identical
meter intervals. A candidate is `ready`
only when both holdout interval MAE and daily MAE improve by at least 1% over factor
1 and the fitted factor is inside the allowed bounds. Neither MAE may worsen against
the incumbent factor, and absolute power/energy bias may not worsen against either
comparator (apart from numerical tolerance). Pin the incumbent activation when the
job is created and retain it in the dataset manifest. Activation fails if the current
selection has changed since that comparison. Old policy candidates cannot be newly
activated; existing activation history remains valid. Rejected candidates cannot
be activated; the current activation remains unchanged.
The 1% margin is an operational acceptance rule, not a significance test. Confidence
intervals are not estimated in this version; do not expose invented probability
bands. No fit activates itself. Compare later unseen days and revisit performance
across seasons before making accuracy or degradation claims.
With at least 30 matched days, report up to three expanding-training, three-day
evaluation folds within the training period. The final seven-day holdout is never
included in these folds. They diagnose historical reference-transfer stability;
they neither tune hyperparameters nor establish operational forecast accuracy.
Report source-positive interval MAE separately, using whole meter intervals with
positive reference power; it is not an astronomical daylight classification.

The dataset pairs manual metered production or settlement injection with matching Solcast estimated actuals,
never archived forecasts. Select the latest matching acquisition published by the
request cutoff and retain its snapshot, source kind, collection/publication times,
configuration and dataset digest. Pin the selected reference and settlement
snapshot IDs when requesting a new run; later acquisitions cannot enter its
retries, even with a backdated timestamp. Existing legacy runs retain their
recorded cutoff policy. Only completed local days enter calibration;
future production intervals are rejected at import. Importing an old measurement
explicitly associates it with the declared configuration, without backdating that
configuration. Do not mix periods with different capacities or meter definitions.

The initial seven-day provider window can contain fewer than seven complete local
days. Wait for enough matched days rather than relaxing the minimum. The new
selection policy is `latest-complete-estimated-actuals-v2`: prefer the newest
acquisition with complete valid reference PV power coverage of the selected local
day, otherwise return the newest partial acquisition. Never merge vintages. This
prevents the oldest day of a rolling 168-hour window from hiding an earlier whole
day. Completion requires finite nonnegative power and contiguous, non-overlapping
elapsed-time coverage, including valid nighttime zeroes and 23/25-hour local days;
it does not require reconstructed GHI to be identifiable. Night collection starts
at 03:00 and retries incomplete or failed input at 30-minute intervals before
09:00, within the account's available budget after scheduled forecasts. Old
requests retain pinned acquisitions; already active factors remain historical
selections, while old-policy ready candidates require a new request before
activation. Old forecast-based
candidates cannot be newly activated as this calibration. Existing activation
history remains readable. Activation applies only after its recorded time, so
archived forecasts retain the factor available then. Holdout improvement against
estimated actuals does not establish improvement in future forecast accuracy.

Meter entry requires matching archived estimated actuals for the entire interval.
Saving at least seven days with complete valid matched daylight coverage automatically
schedules fitting; daylight gaps and outage labels do not satisfy this threshold.
Use a 90-day window ending on the latest recorded local day and elapsed durations, including DST
days. Capture both an observation timestamp cutoff and an ID watermark so readings
saved during fitting cannot enter that dataset, even with identical timestamps.
New readings remain saved and can prepare a subsequent candidate. Preparation never
activates a factor automatically.

The versioned `astral-3.2-sea-level-v1` policy uses the apparent upper solar limb,
standard refraction and a sea-level horizon, without terrain or shading inference.
Collect UTC sunrise/sunset events around the local day to preserve DST, date-line
and midnight-crossing daylight. Polar day requires the full local day; polar night
does not count as a calibration day. The journal and calibration use the same
windows. A time interval ending exactly at sunrise or starting at sunset has no
daylight overlap. The journal hides night slots but retains original saved readings.

## Archived forecast evaluation

The live current/future outlook uses the latest eligible published forecast and
current configuration/calibration. It can change at each collection time and is
not written over a historical forecast. The archived evaluation below retains
the separate day-ahead information cutoff; live intraday updates do not enter it.

The plant performance endpoint replays archived day-ahead forecast inputs against
meter energy for 1–90 completed local days. It uses only the current configuration's
observations, and excludes days before that configuration existed. Select the first
forecast acquired on the preceding local day, published before the target day,
with a successful collection and no later provider issue time. Use only the
calibration activated and completed before that day. Never fall back to estimated
actuals or a retrospectively declared configuration.

Mark this as `historical_input_replay`: it does not prove that an electrical output
was actually published at that historical time. Compare the selected historical
factor and factor 1 on the same complete, valid days. With `daylight_only=true`,
require complete daylight coverage and report `metric_scope: daylight_intervals`;
this is the frontend default. The API's full-day mode retains 24-hour coverage and
metrics. Both modes preserve whole meter boundary intervals. Keep valid zero-production
and zero-source days in evaluation, including their errors; do not infer outages
from zero alone. Source-positive MAE is null when no such intervals exist.
Return daily measured/predicted/baseline MWh, signed error, snapshot and calibration
identities, excluded-day reasons and aggregate MAE/bias. Missing days cannot become
zero errors. This diagnostic does not train or activate a model and does not produce
confidence intervals from a short or selectively observed history.

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
production remains excluded from fitting and forecast-performance metrics.

Policy `manual-over-settlement-hour-v1` supplements manual observations with the
latest available UEVM acquisition per day for the current configuration and
provider identity, only for a `net_export` target. This target includes settlement
adjustments; it does not assert raw connection-meter equivalence. Any overlapping
manual observation, including invalid/outage/curtailed readings, suppresses the
entire provider hour. Missing or invalid provider values remain missing, and the
existing daylight-coverage rules decide which days qualify. Do not split hourly
energy into invented half-hour measurements.

Record settlement snapshot IDs, provider identity and digests in the dataset
manifest. A stable fingerprint of measurement values, quality, source identity,
configuration and matched reference content controls automatic refitting;
unchanged repeated acquisitions do not trigger a new fit. Preserve the request's
selected snapshot IDs separately for replay. Corrected inputs can prepare a new
candidate, while completed fits and archived forecasts remain unchanged.

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
