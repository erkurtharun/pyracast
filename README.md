# Pyracast

Multi-tenant irradiance forecasting backend. Product scope and boundaries live in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); calculation contracts live in
[docs/IRRADIANCE.md](docs/IRRADIANCE.md). The backend includes a session-authenticated
tenant API, MFA-protected operations interface, PostgreSQL jobs and quota ledger,
RabbitMQ/Dramatiq entry points, archived vintages and an explicitly unvalidated
irradiance baseline. Synthetic fixtures exercise the complete application flow.

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
coverage. The floor is 100%, measured on 2026-11-09; never lower it. Settings live in
`pyproject.toml`. The package smoke test only
proves package installation/import; behavioral tests cover tenant isolation, MFA/CSRF,
quota, leases, concurrent approvals, redelivery, stale revisions and numerical invariants
against real PostgreSQL and synthetic provider data. CI also builds and imports the
wheel outside the checkout on Windows and Ubuntu. CI uses GitHub-hosted
`ubuntu-latest` and `windows-latest` runners;
no self-hosted runner or organization-specific reusable workflow is required.
PyYAML 6.0.3 currently needs a source build on CPython 3.15; this was tested on
Windows. CI permits this locked build. No dependency or interpreter was downgraded.

CI audits locked runtime, optional scientific and development dependencies against
public vulnerability advisories. To reproduce:

```text
uv export --locked --all-groups --all-extras --no-emit-project --no-hashes --output-file audit-requirements.txt
uv run --locked pip-audit --strict --no-deps --disable-pip --requirement audit-requirements.txt
```

Dependabot proposes weekly uv and GitHub Actions updates; workflow actions are
pinned to immutable commits. No auto-merge or deployment is configured.

## Run locally

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

The dispatcher scans durable jobs every 60 seconds. Queue publication can be
repeated; database leases and unique results govern execution. Requests, validation
and retries share one account/UTC-day budget. Unknown request outcomes consume quota.
Provider calls happen outside transactions. Changed or suspended regions cannot
publish through old bindings. Failed calculations can reuse an archived acquisition.

## Operations and API

All tenant routes require membership in the path's tenant; request bodies cannot
choose ownership or approval status. Create tenant memberships through trusted
operator provisioning, for example the Django shell (`Tenant`, `Membership`, built-in
`User`). The development fixture command provisions its own tenant membership.

| Route | Operation |
| --- | --- |
| `/api/tenants/` | GET the authenticated user's memberships; default login destination |
| `/api/tenants/{tenant}/regions/` | GET paginated regions; POST region request |
| `/api/tenants/{tenant}/regions/{region}/` | GET; PUT replacement settings, requiring reapproval |
| `/api/tenants/{tenant}/regions/{region}/forecast/?date=YYYY-MM-DD` | Saved vintage, intervals, coverage and daily/partial energy |
| `/api/platform/accounts/` | POST canonical authorized account and write-only secret reference |
| `/api/platform/accounts/{account}/rotate-secret/` | POST write-only `secret_reference`; revalidation required |
| `/api/platform/regions/{region}/bindings/` | POST immutable reference configuration |
| `/api/platform/bindings/{binding}/validate/` | POST quota-counted access validation |
| `/api/platform/regions/{region}/approve/` | POST `{ "binding_id": 1 }` |
| `/api/platform/regions/{region}/transition/` | POST rejected or suspended status |
| `/api/schema/?format=json` | Authenticated OpenAPI document |
| `/admin/` | MFA-protected read-only records and shared approval actions |

Platform operations require an active superuser and a verified OTP session. Enroll
the initial superuser's TOTP device through the trusted local Django shell before
admin login: create a confirmed `django_otp.plugins.otp_totp.models.TOTPDevice` for
that user and transfer its `config_url` to their authenticator through a private
channel. Treat the enrollment URI as a secret. Password-only sessions cannot call
platform operations. Sessions enforce CSRF on writes; use the CSRF cookie/header.

Solcast accounts require an `authorization_reference` documenting permission to use
that account for this service. Set `secret_reference` to an environment secret name
such as `env:PYRACAST_SOLCAST_REFERENCE`; supply its value outside the database.
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

## Verification boundaries

The CPython 3.15.2 CI matrix checks locked installation, PostgreSQL 18.6 migrations,
authenticated API behavior, numerical calculations, a real RabbitMQ task and isolated
wheel installation on Windows and Ubuntu. Windows uses native PostgreSQL and RabbitMQ;
Ubuntu uses their pinned containers. Local Windows and Ubuntu CI checks have exercised
the application against real PostgreSQL. StubBroker tests additionally cover worker
behavior without a broker, but do not establish RabbitMQ compatibility.
Set `PYRACAST_LIVE_QUEUE=1` and a dedicated `PYRACAST_RABBITMQ_URL` to run that test
locally. It uses the dedicated broker's default queue. Without that explicit
opt-in, the real-broker test reports a skip. Native Windows ACL tests are explicitly
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

An authorized live check on 2026-11-09 used two Bearer-authenticated requests:
one saved-site lookup and one forecast acquisition. Both succeeded. The saved
coordinates, numeric capacity (1 kW), tilt (0 degrees) and loss factor (1) matched
the supplied reference. The response did not establish AC/DC capacity semantics,
snow/shading configuration or a timezone; the requested region timezone is
Europe/Istanbul. No live binding was automatically approved from incomplete evidence.
The captured installation timestamp is retained in the ignored local evidence.

The sanitized [forecast fixture](tests/fixtures/solcast_forecasts.json) contains
97 half-hour intervals; its [metadata](tests/fixtures/solcast_forecasts.metadata.json)
records collection time. It contains no matched GHI or provider issue time.
Reconstructed GHI remains **unvalidated**. Routine tests replay fixtures and never
consume provider quota. A successful HTTP call establishes integration only.

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

The `Sonar quality gate` job consumes Ubuntu's `coverage.xml`, fetches full Git
history and fails when the configured Sonar quality gate fails. Before enabling,
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
