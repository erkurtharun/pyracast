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

## Primary references

- [Hobby contract](https://docs.solcast.com.au/docs/section/rooftop-sites-hobbyist).
- [Rooftop model](https://www.solcast.com/rooftop-pv-power-model).
- [Irradiance and transposition methodology](https://www.solcast.com/irradiance-data-methodology).
