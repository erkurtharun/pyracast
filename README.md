# Pyracast

Multi-tenant irradiance forecasting application. Product scope and boundaries live in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); calculation contracts live in
[docs/IRRADIANCE.md](docs/IRRADIANCE.md). The backend includes a session-authenticated
tenant API, MFA-protected operations interface, PostgreSQL jobs and quota ledger,
RabbitMQ/Dramatiq entry points, archived vintages, plant generation with native
interval clipping and offline production calibration. Irradiance reconstruction
and the initial plant transfer remain explicitly unvalidated proxies. Synthetic
fixtures exercise the application flow. The `web/` Next.js interface provides region,
plant, production, calibration and saved-forecast email workflows.

## Development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run these
commands from the repository root in PowerShell or a Unix shell:

```text
uv sync --locked
uv run --locked python scripts/check.py
uv build --no-sources
```

`.python-version` and package metadata require CPython 3.15.2. `uv.lock` pins the
resolved stable application and development dependencies. Use
`uv lock --upgrade` for deliberate upgrades, review the lock diff and rerun checks.
`uv run --locked ruff format .` formats code.

The check command runs formatting, Ruff, strict mypy and parallel pytest with branch
coverage. The required floor is 100%; never lower it. Settings live in
`pyproject.toml`. The package smoke test only
proves package installation/import; behavioral tests cover tenant isolation, MFA/CSRF,
quota, leases, concurrent approvals, redelivery, stale revisions and numerical invariants
against real PostgreSQL and synthetic provider data. CI also builds and imports the
wheel outside the checkout on Windows and Ubuntu. CI uses GitHub-hosted
`ubuntu-latest` and `windows-latest` runners;
no self-hosted runner or organization-specific reusable workflow is required.
The uv policy blocks unlisted third-party source builds and explicitly permits the
locked PyYAML and h5py builds required on CPython 3.15. Cached wheels may still be
reused; first-party project builds remain permitted.

CI audits locked runtime, optional scientific and development dependencies against
public vulnerability advisories. To reproduce:

```text
uv export --locked --all-groups --all-extras --no-emit-project --no-hashes --output-file audit-requirements.txt
uv run --locked pip-audit --strict --no-deps --disable-pip --requirement audit-requirements.txt
```

Dependabot proposes weekly uv and GitHub Actions updates; workflow actions are
pinned to immutable commits. No auto-merge or deployment is configured.

## Run locally

On the configured Windows development machine, double-click **`start.cmd`**.
It checks PostgreSQL/RabbitMQ, installs locked dependencies when needed, applies
migrations, starts the API, frontend, worker and dispatcher, then opens
`http://127.0.0.1:3000`. Existing workspace processes are reused; a foreign process
on ports 3000/8000 is an error, never terminated or assigned a different port.
The first frontend installation takes longer; later starts reuse the lockfile cache.

Double-click **`stop.cmd`** to stop the four application processes, or
**`status.cmd`** to inspect them. Stop waits up to five minutes for active worker
jobs. PostgreSQL and RabbitMQ are shared dependencies and remain running; database
files are never deleted or reinitialized. Windows termination after the drain is
bounded process termination, not a guarantee of graceful signal handling.
For terminal use, from either Windows or Ubuntu:

```text
uv run --locked python -m scripts.dev start
uv run --locked python -m scripts.dev status
uv run --locked python -m scripts.dev stop
```

Use `stop --frontend-only` to close the interface while automatic collection keeps
running. The launcher uses the existing root `.env`, uv and Node.js 26.13.0+.
Logs and process identities are local to ignored `.dev/`; the process registry
stores no passwords or connection URLs. A successful first start records existing native Windows dependency paths
so stopped dependencies can be restarted using the same files. If those files are
missing or no recipe exists, it fails rather than creating an empty database.
On Ubuntu and for remote dependencies, provide running PostgreSQL/RabbitMQ using
the setup below; automatic dependency recovery is Windows-only. The process
launcher has native OS tests in CI; a Windows smoke run does not establish Linux
dependency recovery. `start.cmd --no-browser` suppresses opening a browser.

Provide PostgreSQL, copy `.env.example` to an ignored `.env` and fill its values.
Every process loads `.env` from its working directory without overriding existing
environment variables or expanding values. Start processes from the repository root.
Alternatively, use `$env:NAME='value'`
in PowerShell or `export NAME='value'` in a POSIX shell. These settings apply to each
API, dispatcher and worker process.

| Variable | Local development value |
| --- | --- |
| `PGHOST` | `127.0.0.1` |
| `PGPORT` | `55432` for the helper below, otherwise your database port |
| `PGDATABASE`, `PGUSER` | `pyracast` |
| `PGPASSWORD` | Your PostgreSQL password |
| `PYRACAST_SECRET_KEY` | A long random application secret, shared between processes |
| `PYRACAST_SECRET_KEY_FALLBACKS` | Optional space-separated previous keys during rotation; retain until stored provider credentials have been re-saved under the new primary key |
| `PYRACAST_DEBUG` | `1` for local HTTP only |
| `PYRACAST_HOSTS` | `localhost,127.0.0.1` |
| `PYRACAST_CUTOFF_LOCAL` | `16:00` (local `HH:MM`) |
| `PYRACAST_RABBITMQ_URL` | Your authorized AMQP connection URL |

For an isolated **development/test** PostgreSQL instance, run
`uv run --locked python scripts/start_postgres.py`. It downloads checksum-pinned
PostgreSQL 18.6 binaries on Windows or uses a digest-pinned image through Docker on
Ubuntu. It binds to loopback and uses passwordless trust authentication; never use
this helper for production. It leaves the temporary database running for the session.
An existing database can be used instead. Tests require permission to create test databases.

```text
uv run --locked pyracast migrate
uv run --locked pyracast createsuperuser
uv run --locked pyracast fixture_demo --username YOUR_SUPERUSER
uv run --locked pyracast runserver --noreload
```

`fixture_demo` prints the saved forecast URL. Sign in at `/auth/login/`, then open
that URL. This development command executes the durable job directly; it does not
test RabbitMQ. The synthetic day yields 7.2 kWh/m² and carries `synthetic_fixture`
and `unvalidated` flags. It is not a Solcast forecast or an accuracy reference.

For queued processing, provide RabbitMQ. On Windows,
`uv run --locked python scripts/start_rabbitmq.py` starts an isolated, loopback-only
development broker from checksum-pinned RabbitMQ 4.4.0 and Erlang/OTP 29.1.2 archives.
On Ubuntu with Docker, start the pinned broker image:

```text
docker run -d --rm --name pyracast-rabbitmq -p 127.0.0.1:5672:5672 rabbitmq:4.4.0@sha256:485319d42170eab53d8a030e1cc49d95e41f5d325479fa504736db8091981eb7
```

These are development services; use your configured broker URL in production. Then
run these in separate terminals:

```text
uv run --locked pyracast dispatch --loop
uv run --locked pyracast worker --threads 4
```

The dispatcher scans durable forecast, EPİAŞ production, calibration and notification jobs every 60 seconds. Queue publication can be
repeated; database leases and unique results govern execution. Solcast requests, validation
and retries share one account/UTC-day budget. Unknown request outcomes consume quota.
Provider calls happen outside transactions. Changed or suspended regions cannot
publish through old bindings. Failed calculations can reuse an archived acquisition.

In **Data collection**, choose daily local forecast times for each approved region.
Previous-day history is collected independently at 03:00 local time with
`hours=168`, retaining the available completed seven-day history. Missing or failed
data retries no sooner than every 30 minutes before 09:00, with Solcast quota
reserved for scheduled forecasts. Forecast and Solcast history requests share the account's UTC quota.
Keep the dispatcher and worker
running for scheduled collection; changing times does not fetch data in the web request.
Email reminders use saved forecasts and make no provider requests.

## Customer interface

The independently running Next.js app is in `web/`. Use Node 26.13.0 LTS and npm;
exact compatible stable versions live in `web/package.json` and `package-lock.json`.
The locked TypeScript version must satisfy the OpenAPI generator's peer dependency.
Upgrades must preserve a valid dependency tree.

Start Django on port 8000 with `PYRACAST_DEBUG=1`. Add
`PYRACAST_CSRF_ORIGINS=http://localhost:3000 http://127.0.0.1:3000` to the ignored
root `.env`, then run:

```text
npm --prefix web ci --ignore-scripts
npm --prefix web run dev
```

Open `http://127.0.0.1:3000` and sign in with an existing Django user. The user's
tenant memberships determine which regions are visible. For another API address,
copy `web/.env.example` to ignored `web/.env.local` and set `DJANGO_API_ORIGIN`.
The API origin is a server setting, never a browser credential. Frontend text is English.

For production, run `npm --prefix web run build` and `npm --prefix web start` behind
an HTTPS gateway. Route `/api/` to Django on the same public origin, disable gateway
caching for authenticated responses and rate-limit login attempts. If the gateway
terminates TLS, set `PYRACAST_TRUST_PROXY=1` only when it strips and sets
`X-Forwarded-Proto` and the Django service is reachable only through trusted infrastructure.

```text
npm --prefix web exec -- playwright install chromium
npm --prefix web run check
```

The browser suite includes mocked error/empty-state and accessibility checks.
CI measures authored frontend TypeScript with production-browser V8 coverage:
`npm --prefix web run check:coverage` writes `web/coverage/lcov.info`. Unexecuted
sources remain in the report. Source maps are enabled only for this isolated build.
CI also sets `PYRACAST_BROWSER_TEST=1` and runs
`uv run --locked python -m pytest tests/test_browser.py -n 1 --no-cov` against a
real Django server and isolated PostgreSQL test database. No live provider or mail
service is used. Regenerate the contract after API changes:

```text
uv run --locked pyracast spectacular --validate --fail-on-warn --format openapi-json --file web/openapi.json
npm --prefix web run api:generate
```

## Automatic plant production

Set `EPIAS_USERNAME` and `EPIAS_PASSWORD` in the ignored root `.env`, apply migrations,
and restart the API and worker. In a plant's **Automatic production** tab, enter its
EPİAŞ plant ID and EIC. The backend verifies their match and queues the previous
90 completed Istanbul dates plus twelve completed months of settlement injection
when its matching EIC is available. Keep `dispatch --loop` and `worker --threads 4`
running; the independent 03:00 local collection reconciles seven dates for late or
revised reports. Yesterday's incomplete or failed production retries at 30-minute
intervals before 09:00, respecting longer provider retry hints. No Solcast request
is needed for this archive.

Reported hourly MWh and missing values remain separate from manual production.
Disconnect preserves saved acquisitions. Calibration uses manual net-export
readings, then settlement injection, then provisional reported production under
the [source policy](docs/IRRADIANCE.md). Later settlement values replace provisional
hours in the current view and prepare a new calibration candidate. Prior fits
remain unchanged. In **Calibration**, enable automatic calibration once to schedule
daily checks and guarded activation. Disabling keeps the last good model; resetting
to baseline disables automation. New configurations need fresh authorization.
The fitter compares 30/60/90-day training windows before an untouched holdout.
Automatic activation also needs sufficient current Hobby source evidence; imported
historical data alone does not prove live-source accuracy. See the
[calibration policy](docs/IRRADIANCE.md#production-calibration).

An admin can archive an authorized Historical Rooftop PV JSON export without API
quota. Supply a JSON manifest with these exact fields (use the verified source
settings, not values inferred from its filename):

```json
{
  "product": "historical_rooftop_pv",
  "unit": "kW",
  "timestamp_convention": "period_end",
  "period": "PT30M",
  "latitude": 37.80375,
  "longitude": 34.460913,
  "capacity_kw": 1000,
  "dc_capacity_kw": 1000,
  "tilt_degrees": 0,
  "azimuth_degrees": 0,
  "losses_percent": 0,
  "mounting_type": "fixed",
  "source_reference": "Authorized export settings verified by the operator"
}
```

```text
uv run --locked pyracast import_historical_reference --username ADMIN --tenant-id TENANT --region-id REGION --binding-id BINDING --power-file docs/export.json --manifest docs/export-settings.json
```

Run from the repository root; both canonical JSON paths must stay inside it.
Existing acquisitions remain unchanged, and the archive records its actual
import time. Corrected exports create new immutable acquisitions.

Routine tests use synthetic and sanitized captured responses. Live checks are
opt-in: set `PYRACAST_EPIAS_LIVE=1`, `EPIAS_LIVE_PLANT_ID` and `EPIAS_LIVE_EIC`, then
run `uv run --locked pytest tests/test_epias_live.py --no-cov`.

## Email reminders

Choose an approved region's **Email reminders** tab, enter local delivery times,
and optionally override its recipient. Otherwise the address in **Account settings**
is used. Emails use the latest saved forecast; they make no Solcast requests.
Timing, plan limits, stale-data and retry contracts are in
[ARCHITECTURE.md](docs/ARCHITECTURE.md#saved-forecast-email-reminders).

Set `PYRACAST_EMAIL_HOST`, `PYRACAST_EMAIL_PORT`, `PYRACAST_EMAIL_USER`,
`PYRACAST_EMAIL_PASSWORD`, and `PYRACAST_FROM_EMAIL` in ignored `.env` for your
authorized SMTP service. STARTTLS defaults to port 587; implicit TLS uses port 465,
`PYRACAST_EMAIL_TLS=0` and `PYRACAST_EMAIL_SSL=1`. Django's `MAILERS` configuration
uses a bounded timeout. Run the dispatcher and worker commands above continuously.
The 60-second dispatch interval and mail-server latency mean delivery is not an
exact-to-the-second guarantee. Inspect recent delivery states in the region UI.
External SMTP acceptance and inbox delivery require separate live verification.

Large historical Solcast downloads remain local and ignored under `docs/json_*.json`;
they are not frontend assets or routine test fixtures.

## Backend operations and API

All tenant routes require membership in the path's tenant; request bodies cannot
choose ownership or approval status. Company registration creates a new workspace
and its first membership. Additional memberships use trusted operator provisioning,
for example the Django shell (`Tenant`, `Membership`, built-in `User`). The development
fixture command provisions its own tenant membership.

| Route | Operation |
| --- | --- |
| `/api/session/register/` | POST company name, username, email and password; CSRF required |
| `/api/tenants/` | GET the authenticated user's memberships; default login destination |
| `/api/tenants/{tenant}/sites/` | GET the paginated plant portfolio; POST location and technical details atomically |
| `/api/tenants/{tenant}/sites/{region}/` | GET the combined location, approval and plant record |
| `/api/tenants/{tenant}/regions/` | GET paginated regions; POST region request |
| `/api/tenants/{tenant}/regions/{region}/` | GET; PATCH name; DELETE the plant site to the recycle bin |
| `/api/tenants/{tenant}/regions/{region}/location-change-requests/` | GET requests; POST a location change for administrator review |
| `/api/tenants/{tenant}/regions/{region}/forecast/?date=YYYY-MM-DD` | Saved vintage, intervals, coverage and daily/partial energy |
| `/api/tenants/{tenant}/regions/{region}/plants/` | GET the active plant; POST its declaration or approved configuration |
| `/api/tenants/{tenant}/regions/{region}/plants/{plant}/` | GET; PUT a new configuration revision |
| `.../plants/{plant}/observations/` | GET records; POST an atomic MWh/kWh batch |
| `.../plants/{plant}/calibrations/` | GET fit summaries; POST a durable calibration request |
| `.../plants/{plant}/calibrations/{calibration}/` | GET evaluation and reproducible dataset manifest |
| `.../plants/{plant}/calibrations/{calibration}/activate/` | POST `{}` to activate an eligible fit |
| `.../plants/{plant}/calibration/reset/` | POST `{}` to restore the baseline |
| `.../plants/{plant}/forecast/?date=YYYY-MM-DD&resolution_minutes=60` | Electrical interval/daily energy and separate regional irradiance |
| `.../plants/{plant}/performance/?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` | Archived day-ahead input replay against meter energy, up to 90 completed days |
| `/api/platform/accounts/` | POST canonical authorized account and write-only secret reference |
| `/api/platform/accounts/{account}/rotate-secret/` | POST write-only `secret_reference`; revalidation required |
| `/api/platform/regions/{region}/bindings/` | POST immutable reference configuration |
| `/api/platform/bindings/{binding}/validate/` | POST quota-counted access validation |
| `/api/platform/regions/{region}/approve/` | POST `{ "binding_id": 1 }` |
| `/api/platform/regions/{region}/transition/` | POST rejected or suspended status |
| `/api/schema/?format=json` | Authenticated OpenAPI document |
| `/admin/` | MFA-protected plant review, private provider setup, validation and approval |

Platform operations require an active superuser and a verified OTP session. Enroll
the initial superuser's TOTP device through the trusted local Django shell before
admin login: create a confirmed `django_otp.plugins.otp_totp.models.TOTPDevice` for
that user and transfer its `config_url` to their authenticator through a private
channel. Treat the enrollment URI as a secret. Password-only sessions cannot call
platform operations. Sessions enforce CSRF on writes; use the CSRF cookie/header.

Companies can create an account from the sign-in page, then add a plant through
Location → Technical details → Review. Each location supports one active plant.
The request remains visible while an administrator sets up and approves its data
connection. The customer's workspace never requests provider credentials.

In `/admin/`, open **Regions → Review and set up** for the submitted plant. Check
the location and technical details, select an existing authorized account or add
a new Solcast account, save the reference settings, then **Validate connection**
and **Approve plant**. Validation makes a provider request within the existing quota;
saving a form does not. Approval queues collection for the worker.

Solcast accounts require an `authorization_reference` documenting permission to use
that account for this service. The admin form accepts a write-only API key, encrypted
in the database with a key derived from `PYRACAST_SECRET_KEY`. Keep this external
secret with your backups. During rotation, retain the old key in
`PYRACAST_SECRET_KEY_FALLBACKS`, re-save each stored provider key under the new primary
key, and then remove the fallback. Alternatively, set `secret_reference` to an
environment name such as `env:PYRACAST_SOLCAST_REFERENCE` and supply its value outside
the database.
Credential rotation uses the protected API or `connections.operations.rotate_secret`
and invalidates previous validation without resetting quota. Account creation and
rotation write atomic audit events without secret values or references. Canonical account identity remains
the same across rotations and tenants. Do not invent alternative identities.

Binding creation records capacity and its AC/DC basis, installation date, azimuth,
losses, coordinates and timezone. Solcast bindings additionally require a
`settings_verification_reference`: operator evidence that the saved Toolkit settings
match this entire configuration, including horizontal tilt and available shading/snow
behavior. API access validation cannot independently verify those Toolkit settings.
`capacity_semantics_verified` must be true. No accounts or sites are created at Solcast.

Scheduling uses `PYRACAST_CUTOFF_LOCAL` (default 16:00) in each region's timezone and
recovers the most recent missed slot. A nonexistent local cutoff moves forward by
the DST gap; a repeated cutoff uses its first occurrence. Responses expose
`scheduled_slot` and `late` (collection completed after that
slot). Freshness means the approved binding's acquisition is at most 24 hours old;
completeness is reported separately. Historical results retain their source timezone.
Incomplete days have a null daily total and an available partial total. A local day
can contain 23 or 25 hours. Unknown daylight zero power is not filled as zero GHI.

Production requires HTTPS, a deployment secret and explicit hosts/CSRF origins.
Secure cookies and HTTPS redirects are enabled by default. Serve `pyracast.wsgi`
with a suitable WSGI server and collect static assets; `runserver` is development only.

## Plant production and calibration

After region approval, configure a plant using a member's username. This example
means 130 MW DC modules and a 100 MW **net export** limit; it does not invent an
inverter rating. Add `--inverter-capacity-mw` when known. Use `ac_generation` for
inverter-side metering instead. The command prints the plant and configuration IDs:

```text
uv run --locked pyracast configure_plant --username YOUR_USER --region-id REGION_ID --name "Reference plant" --dc-capacity-mw 130 --export-limit-mw 100 --measurement-basis net_export
```

Add `--plant-id PLANT_ID` to revise an existing plant. Capacity or meter changes
create a new version; its calibration starts from the baseline. Forecast GET accepts
`resolution_minutes=30` or `60` and optional aware `as_of`. A requested historical
time must have an existing configuration. Sources acquired during or after the target
day cannot turn into day-ahead predictions. Predictions retain the first preceding-day
acquisition and its actual lead time. Use `data_source=auto` to prefer estimated
actuals on completed past days, or explicitly choose `forecast` / `estimated_actuals`.
Past electrical estimates use the declared configuration and baseline factor, with
source and configuration-assumption flags; they are not meter readings.

Place a CSV inside the working directory (an ignored local directory such as
`.codex/production.csv` is suitable). Values are **interval energy**, not instantaneous
MW or cumulative meter readings. Include every interval, including measured night
zeros; mark outages/curtailment/invalid data explicitly. For example:

```csv
starts_at,ends_at,energy,quality,source
2026-11-01T12:00:00+03:00,2026-11-01T13:00:00+03:00,72.4,valid,plant-meter
```

```text
uv run --locked pyracast import_production --username YOUR_USER --plant-id PLANT_ID --configuration-id CONFIGURATION_ID --unit MWh --file .codex/production.csv
uv run --locked pyracast calibrate_plant --username YOUR_USER --plant-id PLANT_ID --configuration-id CONFIGURATION_ID --start-date 2026-11-01 --end-date 2026-11-07 --run
```

The sample row is illustrative; it is not sufficient calibration data. Supply at
least seven complete local days matched to saved estimated actuals; a month is preferable for more weather
variation. CSV files accept 30/60-minute intervals, at most 2,000 rows and 2 MiB.
Imports are atomic and exact replay is safe; conflicting records are rejected.
`--run` processes the saved job locally without RabbitMQ or provider calls; omit it
to let the dispatcher/worker process the request. No training occurs in API requests.

Inspect the calibration detail endpoint for baseline/candidate/incumbent errors,
training support and rejection reasons. Five training days must each have enough
uncensored source exposure. Only a `ready` current-policy candidate can be activated;
fits with seven days of complete daylight coverage remain `provisional`, and a
changed incumbent requires a new comparison:

```text
uv run --locked pyracast activate_calibration --username YOUR_USER --plant-id PLANT_ID --calibration-id CALIBRATION_ID
uv run --locked pyracast activate_calibration --username YOUR_USER --plant-id PLANT_ID --baseline
```

Activation/rollback affect subsequent forecast issuance, preserving earlier results.
Manual activation also enables subsequent guarded automatic updates. Automatic
activation follows the stricter source/holdout policy above; no artificial accuracy
percentage is provided. Historical meter data without matching archived estimated
actuals yields `insufficient`.
The production journal selects one local day and shows only whole 30/60-minute
intervals overlapping daylight. Night readings are not invented.
The Forecast performance panel independently compares archived day-ahead inputs
with meter energy on the same daylight intervals; it identifies historical replay
rather than claiming that the plant output was published then. Valid zero-production
days remain in its errors.
Solcast historical estimated irradiance is a different input: it can
support weather-normalized plant analysis when paired with production and geometry,
but it is not accepted as an archived forecast or a meter reading. See
[the calculation contract](docs/IRRADIANCE.md) for the statistical policy and limits.

## Verification boundaries

Forecast worker deliveries can additionally save an independent hourly Open-Meteo
GHI forecast with temperature and ground-snow indicators. This uses only the
existing Hobby endpoints at Solcast. `PYRACAST_WEATHER_ENABLED=1` enables the
auxiliary request; set it to `0` to disable it. The default Open-Meteo endpoint
supports evaluation/non-commercial use. An authorized commercial key can be set
as `PYRACAST_OPEN_METEO_API_KEY`, which selects the customer endpoint. Regional
views keep the two estimates separate and include the required attribution.
See the [irradiance contract](docs/IRRADIANCE.md#live-auxiliary-weather) for units,
timestamp conventions, missingness and the limits of snow-loss inference.

The CPython 3.15.2 CI matrix checks locked installation, PostgreSQL 18.6 migrations,
authenticated API behavior, numerical calculations, a real RabbitMQ task and isolated
wheel installation on Windows and Ubuntu. Windows uses native PostgreSQL and RabbitMQ;
Ubuntu uses their pinned containers. Local Windows and Ubuntu CI checks have exercised
the application against real PostgreSQL. StubBroker tests additionally cover worker
behavior without a broker, but do not establish RabbitMQ compatibility.
Set `PYRACAST_LIVE_QUEUE=1` and a dedicated `PYRACAST_RABBITMQ_URL` to run that test
locally. These tests use the dedicated broker's default and calibration queues. Without that explicit
opt-in, the real-broker tests report skips. Native Windows ACL tests are explicitly
skipped on Ubuntu.

The latest resolved scientific stack is available as the `scientific` extra:
pvlib 0.16.1, NumPy 2.5.4, pandas 3.1.1, SciPy 1.18.1 and h5py 3.16.0. h5py has no
CPython 3.15 Windows/Ubuntu x64 wheel. CI builds it against checksum-pinned official
[HDF5 2.2.0 archives](https://github.com/HDFGroup/hdf5/releases/tag/2.2.0) in a temporary
environment on native Windows and Ubuntu 24.04. Both builds and the pvlib smoke
calculation passed. Reproduce this check with a C compiler installed:

```text
uv run --no-project --python 3.15.2 python scripts/check_scientific.py
```

The helper supplies HDF5, short build paths and platform-specific library loading.
It verifies the linked HDF5 version and checks solar position at 40 N, 29 E on
2026-06-21 12:00 UTC (zenith 29.183607 degrees, azimuth 244.050205 degrees).
This establishes installation and execution, not irradiance accuracy or exhaustive
scientific-stack validation. Installing the extra persistently requires the native
[h5py setup](https://docs.h5py.org/en/stable/build.html); Windows also needs an open
`os.add_dll_directory` handle before importing h5py/pvlib. The default baseline needs
neither HDF5 nor a pvlib surrogate and runs with `uv sync --locked`.

The initial authorized live check on 2026-11-09 used two Bearer-authenticated requests:
one saved-site lookup and one forecast acquisition. Both succeeded. The saved
coordinates, numeric capacity (1 kW), tilt (0 degrees) and loss factor (1) matched
the supplied reference. The response did not establish AC/DC capacity semantics,
snow/shading configuration or a timezone; the requested region timezone is
Europe/Istanbul. Subsequent operator evidence confirmed the Toolkit's inverter AC
capacity and enabled explicit approval of the reference binding. Validation and a
normal collection then succeeded, bringing recorded development requests that UTC
day to four. No matched GHI labels were supplied. The captured installation timestamp
and operator evidence remain local; unknown physical shading/snow losses are not
assumed absent.

The sanitized [forecast fixture](tests/fixtures/solcast_forecasts.json) contains
97 half-hour intervals; its [metadata](tests/fixtures/solcast_forecasts.metadata.json)
records collection time. It contains no matched GHI or provider issue time.
Reconstructed GHI remains **unvalidated**. Routine tests replay fixtures and never
consume provider quota. A successful HTTP call establishes integration only.

On 2026-11-10, two quota-counted Bearer-authenticated history checks succeeded.
The default request returned 97 half-hour records (approximately 48 hours);
`hours=168` returned 337 records spanning the rolling seven-day window. The
sanitized [estimated-actuals fixture](tests/fixtures/solcast_estimated_actuals.json)
and [metadata](tests/fixtures/solcast_estimated_actuals.metadata.json) preserve
that response. Collection trims incomplete local days and retains both acquisitions;
neither this response nor the electrical proxy establishes measured irradiance accuracy.

For another explicitly enabled live check, first register the canonical authorized
Solcast account with `secret_reference=env:SOLCAST_API_KEY`, then run:

```text
uv run --locked python scripts/check_solcast.py --live --account-id ACCOUNT_ID --kind settings
uv run --locked python scripts/check_solcast.py --live --account-id ACCOUNT_ID --kind forecast
```

The tool loads `SOLCAST_API_KEY` and `SOLCAST_RESOURCE_ID` from `.env`, reserves the
shared account/UTC-day budget before HTTP, sends no automatic retries and permits
each check kind only once per UTC day. Sanitized artifacts go to ignored
`.codex/solcast/`; filenames include the attempt ID to preserve separate acquisitions.
Both inspection requests count conservatively toward the local limit of 10.
Solcast returned no remaining-quota headers, so other applications' usage is unknown;
coordinate those consumers before enabling additional live runs. Do not create a
second account record to reset quota or infer AC/DC settings from expected values.

The offline `pyracast.evaluation` module computes matched daily MAE/bias and daylight
GHI error/coverage when valid reference labels become available; it does not train
models or establish seasonal/geographic validity.

Version/contract references: [Django 6.1.3](https://docs.djangoproject.com/en/6.1/releases/6.1.3/),
[Solcast Hobby API](https://docs.solcast.com.au/docs/section/rooftop-sites-hobbyist),
[pvlib distribution](https://pypi.org/project/pvlib/0.16.1/),
[h5py distribution](https://pypi.org/project/h5py/3.16.0/).

## SonarQube Cloud

The GitHub repository is connected to SonarQube Cloud project
[`erkurtharun_pyracast`](https://sonarcloud.io/project/overview?id=erkurtharun_pyracast)
in organization `erkurtharun`. CI-based analysis requires automatic analysis to be
disabled in the project's Administration > Analysis Method. GitHub repository settings use:

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `SONAR_TOKEN` | Token authorized to analyze this Sonar project |
| Variable | `SONAR_ORGANIZATION` | `erkurtharun` |
| Variable | `SONAR_PROJECT_KEY` | `erkurtharun_pyracast` |
| Variable | `SONAR_ENABLED` | `true`, after the values above are configured |

The `Sonar quality gate` job analyzes backend and authored frontend sources, using
Ubuntu's `coverage.xml` and `web/coverage/lcov.info`. Generated API types are excluded
from analysis. It fetches full Git history and fails when the configured quality
gate fails. Before enabling,
the job is explicitly skipped; local checks still run. Fork and Dependabot PRs
run verification without a Sonar token; merge-group events also skip analysis.
Do not use `pull_request_target` to run untrusted PR code with secrets.

The scanner target is honestly set to Python 3.15. Sonar's current
[Python support documentation](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/languages/python)
lists full support through 3.14, so complete analysis of 3.15 is unverified.
The token is stored as a GitHub Actions secret, never in tracked project files.
See the official [coverage configuration](https://docs.sonarsource.com/sonarqube-cloud/enriching/test-coverage/python-test-coverage)
and [scan action](https://github.com/SonarSource/sonarqube-scan-action).

## Agent workflow

Start with [AGENTS.md](AGENTS.md). Shared Codex lifecycle hooks live in
`.codex/hooks.json` and `.codex/hooks/quality.py`. They format only edited Python
files, without semantic lint fixes or repeated test runs on every stop. Run
`scripts/check.py` explicitly before delivery; CI uses the same gate. Read guards
exclude environments, caches and root-level generated output while allowing source
packages and test fixtures named `data`. This is a tool-level
context guard, not a filesystem sandbox; shell commands and dependency loading still work.
Local `.codex/config.toml`, IDE state, secrets and scanner artifacts stay ignored.
Codex requires non-managed hooks to be reviewed and trusted through `/hooks` before
they run; the repository does not bypass that trust mechanism.

Install user-level CLI tools without registering MCP servers:

```text
npm install -g ctx7@0.10.0
uv tool install code-review-graph==2.3.9 --python 3.15.2
ctx7 --version
code-review-graph --version
```

Use `ctx7 library <name> <question>`, then `ctx7 docs <id> <question>` as documentation
leads. Do not run Context7 setup or the graph install/daemon/watch/embed/wiki commands.
The local Codex `[shell_environment_policy.set]` sets `CRG_HOME` to a per-user cache
outside the checkout (Windows: `%LOCALAPPDATA%/code-review-graph/pyracast`; Ubuntu:
choose a directory under `$XDG_CACHE_HOME` or `~/.cache`). Pass the same path to
`code-review-graph build --repo . --skip-flows --data-dir <CRG_HOME>` and subsequent
updates. Queries resolve this external database through the registry in `CRG_HOME`;
they do not support a `--data-dir` flag. No graph database belongs in git.

The local Sonar MCP connection is configured in ignored `.codex/config.toml`.
It uses the official Sonar MCP JAR and the installed Java runtime because Docker
is unavailable on this workstation. Its startup and tool discovery have been
verified. Restart the Codex session to load newly added MCP tools. Other machines
can use either Docker or Java 21+ following the
[official MCP setup](https://github.com/SonarSource/sonarqube-mcp-server).
