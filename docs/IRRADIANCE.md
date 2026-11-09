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

For inverter AC production, apply the known inverter limit. For net export, apply
the smaller known inverter/export limit. Omit unknown limits rather than inventing
them. Thus 130 MW DC and a 100 MW export limit are separate inputs. Apply clipping
at the native source interval **before** hourly aggregation: native powers 80 and
120 MW over two half-hours with a 100 MW limit produce 90 MWh for the hour.
Interval means cannot recover clipping at finer time resolution.

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

Use complete, matched local days with `valid` observations. Exclude days containing
missing data, invalid records, outages or declared curtailment. Do not infer outages
from low output alone. Exclude measurements at or above 98% of the known cap from
training because clipping hides uncapped performance; retain valid clipped intervals
in evaluation. Zero-source samples do not identify a multiplicative factor.

At least seven complete matched days are required. For 7–29 days, reserve the last
max(2, ceil(20% of days)) for evaluation; for 30 or more, reserve the last seven.
Fit only earlier days and do not refit on the holdout. Seven days can check the
pipeline and support a **provisional** fit; a month covers more conditions but
neither duration proves seasonal validity. Intervals within a day are correlated,
so hundreds of half-hours are not hundreds of independent weather realizations.

Report interval power MAE/bias (MW), daily energy MAE/bias (MWh), included/excluded
days, censored counts and the exact chronological split. A candidate is `ready`
only when both holdout interval MAE and daily MAE improve by at least 1% over factor
1 and the fitted factor is inside the allowed bounds. Rejected candidates cannot
be activated; the current activation remains unchanged.
The 1% margin is an operational acceptance rule, not a significance test. Confidence
intervals are not estimated in this version; do not expose invented probability
bands. No fit activates itself. Compare later unseen days and revisit performance
across seasons before making accuracy or degradation claims.

The dataset uses the first forecast acquired during the previous local day, never
estimated actuals or a refresh collected later than the forecast's `as_of`. Store
the source-selection policy, actual collection times, observation cutoff, configuration
and dataset digest. Activation applies only after its recorded time. Historical
forecasts retain the factor actually available then. Production history alone
cannot reconstruct forecasts that were never archived.

## Historical irradiance and measured performance

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
combined forecast-transfer bias; it is **not** an estimate of panel degradation or
a calibration of true irradiance. Historical-irradiance import and a physical
performance model require a verified dataset contract and are separate from this
forecast-transfer calibration. Never use later estimated actuals as future forecast
features or as matched Solcast forecast GHI for reconstruction-equivalence claims.

## Primary references

- [Hobby contract](https://docs.solcast.com.au/docs/section/rooftop-sites-hobbyist).
- [Rooftop model](https://www.solcast.com/rooftop-pv-power-model).
- [Irradiance and transposition methodology](https://www.solcast.com/irradiance-data-methodology).
- [Historical time-series parameters and interval means](https://solcast.com/time-series).
- [PVWatts DC model inputs](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.pvsystem.pvwatts_dc.html) and [inverter clipping](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.inverter.pvwatts.html).
- [Huber regression](https://scikit-learn.org/stable/modules/linear_model.html#huber-regression) and [time-series evaluation](https://otexts.com/fpp3/tscv.html).
- [NREL weather-corrected performance ratio](https://research-hub.nlr.gov/en/publications/weather-corrected-performance-ratio/).
