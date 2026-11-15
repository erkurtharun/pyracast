# Pyracast

Multi-tenant irradiance forecasts and plant energy estimates, with a Django API,
Next.js customer interface and MFA-protected operations workspace. PostgreSQL owns
jobs, quotas and immutable acquisitions; RabbitMQ/Dramatiq runs background work.

- [Architecture and business rules](docs/ARCHITECTURE.md)
- [Calculation, calibration and provider contracts](docs/IRRADIANCE.md)
- [Google Cloud deployment](docs/DEPLOYMENT.md)
- [Contributor and agent instructions](AGENTS.md)

Reconstructed irradiance and plant transfer estimates are model proxies.
Fixture tests and successful provider requests do not establish measured accuracy.

## Development setup

Use CPython **3.15.2**, [uv](https://docs.astral.sh/uv/getting-started/installation/),
Node.js **26.13.0+** and npm. Run commands from the repository root in PowerShell
or a Unix shell:

```text
uv sync --locked
npm --prefix web ci --ignore-scripts
```

Provide PostgreSQL and RabbitMQ, copy `.env.example` to ignored `.env` and fill its
values. Processes load that file from their working directory without overriding
existing environment variables or expanding values. Plant-specific Solcast keys
and reference settings belong in the operations workspace, not `.env`.

For isolated development dependencies:

```text
uv run --locked python scripts/start_postgres.py
```

This starts checksum-pinned PostgreSQL binaries on Windows or a pinned Docker image
on Ubuntu, using loopback-only trust authentication. It leaves the database running;
never use it for production. Tests need permission to create databases.

On Windows, `uv run --locked python scripts/start_rabbitmq.py` starts the pinned
development broker. On Ubuntu with Docker:

```text
docker run -d --rm --name pyracast-rabbitmq -p 127.0.0.1:5672:5672 rabbitmq:4.4.0@sha256:485319d42170eab53d8a030e1cc49d95e41f5d325479fa504736db8091981eb7
```

### Local settings

The complete example is [`.env.example`](.env.example). The main groups are:

| Settings | Purpose |
| --- | --- |
| `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` | PostgreSQL connection; the development helper uses port 55432 |
| `PYRACAST_SECRET_KEY` | Shared application encryption/session secret; preserve it with backups |
| `PYRACAST_DEBUG`, `PYRACAST_HOSTS` | Local debug mode and accepted hostnames |
| `PYRACAST_PUBLIC_ORIGIN`, `PYRACAST_CSRF_ORIGINS` | Frontend origin and allowed mutation origins |
| `PYRACAST_RABBITMQ_URL` | Broker connection |
| `PYRACAST_EMAIL_BACKEND` | `console` for local development; `gmail` or `smtp` for delivery |
| `EPIAS_USERNAME`, `EPIAS_PASSWORD` | Optional shared EP?A? transparency-service credentials |
| `PYRACAST_WEATHER_ENABLED`, `PYRACAST_OPEN_METEO_API_KEY` | Auxiliary weather switch and optional commercial key |

Console mail requires `PYRACAST_DEBUG=1`. Verification/reset codes appear in the API
log; reminders appear in the mailer log. These logs contain usable private codes,
and messages are not delivered to an inbox. Gmail uses only `gmail.send` with
the `PYRACAST_GMAIL_CLIENT_ID`, `PYRACAST_GMAIL_CLIENT_SECRET` and
`PYRACAST_GMAIL_REFRESH_TOKEN` credentials; follow the local authorization steps in
the [deployment guide](docs/DEPLOYMENT.md#2-add-secrets). For SMTP, configure the
`PYRACAST_EMAIL_*` connection values and `PYRACAST_FROM_EMAIL` from your authorized
provider. STARTTLS defaults to port 587; implicit TLS uses port 465,
`PYRACAST_EMAIL_TLS=0` and `PYRACAST_EMAIL_SSL=1`.

Open-Meteo's free service is for evaluation/non-commercial use; commercial operation
needs the [appropriate subscription](https://open-meteo.com/en/pricing) or disabled
weather. EP?A? plant IDs/EIC and weather coordinates remain database records.

## Run locally

On the configured Windows development machine, double-click `start.cmd`.
It checks dependencies, syncs locked packages, migrates the database, starts the
API, frontend, collection worker, mailer and dispatcher, then opens
`http://127.0.0.1:3000`. Use `start.cmd --no-browser` to suppress the browser.
Overlapping launcher commands wait up to ten minutes for the current operation;
the first dependency installation can take a few minutes.

`status.cmd` inspects processes; `stop.cmd` requests graceful shutdown.
PostgreSQL/RabbitMQ remain running. The launcher reuses recorded workspace processes
and rejects foreign processes on ports 3000/8000. Logs and process identities live
in ignored `.dev/`. A shutdown timeout preserves the process identity for a later
stop attempt.

The portable terminal equivalents are:

```text
uv run --locked python -m scripts.dev start
uv run --locked python -m scripts.dev status
uv run --locked python -m scripts.dev stop
```

`stop --frontend-only` leaves background collection running. Dependency recovery
from saved native paths is Windows-only; on Ubuntu or with remote dependencies,
provide running PostgreSQL/RabbitMQ yourself.

For manual startup, run these in separate terminals after migrations:

```text
uv run --locked pyracast runserver --noreload
npm --prefix web run dev
uv run --locked pyracast dispatch --loop
uv run --locked pyracast worker --threads 4
uv run --locked pyracast worker --queues notifications --threads 2
```

The dedicated notification process keeps provider waits and calibration from
occupying mail threads. Use `PYRACAST_DEBUG=1` and allow
`http://localhost:3000 http://127.0.0.1:3000` in `PYRACAST_CSRF_ORIGINS`.
The frontend proxies Django routes to port 8000. For a different development API,
copy `web/.env.example` to ignored `web/.env.local` and set `DJANGO_API_ORIGIN`.

### First administrator and demo

```text
uv run --locked pyracast migrate --noinput
uv run --locked pyracast createsuperuser
uv run --locked pyracast provision_admin_mfa --username YOUR_ADMIN --email admin@example.com
uv run --locked pyracast fixture_demo --username YOUR_ADMIN
```

Scan the private terminal QR code with the administrator's authenticator; use a
Unicode terminal wide enough to show the complete code without wrapping. The
enrollment URI remains available for manual setup if the console cannot display
the code. Keep both out of logs and tickets. Provisioning refuses to replace an
existing factor. Administrators verify email, sign in with password/TOTP, then
confirm MFA and save recovery codes in **Account settings**.

Sign in at `http://127.0.0.1:3000/`. Administrators open `/admin/`; customers open
their plant portfolio. The synthetic demo processes its durable job directly,
without RabbitMQ or provider requests. Its 7.2 kWh/m? output is a fixture,
not an accuracy reference.

## Operations

In `/admin/`, **Overview** lists pending reviews, **Plants** searches plant/company
records, **Operations** shows collection and email outcomes, and **Settings**
controls the shared recipient limit. To approve a plant, review its declaration,
select an authorized Solcast account, save verified reference settings, then
**Validate connection** and **Approve plant**. Validation consumes account quota;
saving setup does not. Approval queues collection for the worker.

Solcast account authorization and Toolkit settings require operator evidence.
The application encrypts write-only API keys and never creates provider accounts.
Leaving an existing key blank preserves it; replacing a shared key requires explicit
confirmation and invalidates validation without resetting quota.

Customers configure forecast times in **Data collection**, EP?A? IDs in
**Automatic production**, calibration in **Calibration**, and saved-forecast emails
in **Settings ? Email reminders**. Keep dispatcher/workers running. Emails make no
provider requests. Historical production, forecast vintages and estimated actuals
remain separate; see the linked contracts for source selection and numerical limits.

Monitor independently of the email service:

```text
uv run --locked pyracast check_delivery_health
```

It emits aggregate JSON and exits nonzero for missing/stale dispatcher progress,
overdue work, provider blocks, failed/uncertain deliveries or incomplete next-day
forecasts. Inspect individual outcomes in **Operations**. An `uncertain` send needs
mail-provider acceptance evidence before any manual resend; provider acceptance is not
proof of inbox delivery.

### Database and secret maintenance

Django migrations are the sole schema owner. Commit model changes with migrations;
never change applied operations to repair data.

```text
uv run --locked pyracast makemigrations --check --dry-run
uv run --locked pyracast migrate --plan
uv run --locked pyracast migrate --noinput
uv run --locked pyracast migrate --check
```

The overridden `migrate` command uses a PostgreSQL session advisory lock, a
five-second lock timeout and a fifteen-minute statement timeout. Use a direct
connection, not transaction-pooled PgBouncer or Django connection pooling.
Reviewed maintenance can override `--lock-timeout-seconds` and
`--statement-timeout-seconds`. Rehearse changes against a verified restored backup;
see [database rollout](docs/ARCHITECTURE.md#database-invariants-and-rollout).

During application-secret rotation, retain the old key in
`PYRACAST_SECRET_KEY_FALLBACKS`, run `uv run --locked pyracast reencrypt_secrets`,
verify provider/MFA decryption, then remove the fallback. Do not discard legacy
authenticators, mark unverified emails verified, or invent account identities to
bypass an upgrade failure. Run `uv run --locked pyracast clearsessions` regularly.

### Administrative data commands

The UI owns ordinary configuration. These commands support authorized imports and
operator workflows; inspect their `--help` for required declarations and limits:

| Command | Purpose |
| --- | --- |
| `configure_plant` | Create/revise capacity, mounting and measurement declarations |
| `import_production` | Atomically import interval energy CSV for a pinned configuration |
| `calibrate_plant` | Queue a durable fit; `--run` executes it locally without RabbitMQ |
| `activate_calibration` | Activate an eligible candidate or restore `--baseline` |
| `import_historical_reference` | Archive an authorized export and verified settings manifest |
| `normalize_reference_capacity` | Create an explicitly authorized capacity-only derived acquisition |

Import paths must stay inside the repository. Use an ignored local directory.
Production CSV values are interval energy, never instantaneous MW or cumulative
meter readings; preserve units, offsets, night zeros and quality labels. Historical
exports need matching source settings and retain their actual import time.
These commands do not establish accuracy or bypass tenant/authorization checks.

The authenticated OpenAPI document at `/api/schema/?format=json` describes the
customer API. `/api/session/` supplies the session projection and masked CSRF token;
mutations send `X-CSRFToken`. Platform operations remain administrator-only.

## Verification

```text
uv run --locked python scripts/check.py
uv build --no-sources
npm --prefix web exec -- playwright install chromium
npm --prefix web run check
```

The backend gate runs formatting, Ruff, strict mypy and parallel pytest with the
100% branch-coverage floor configured in `pyproject.toml`. Use
`uv run --locked ruff format .` for formatting. Deliberate dependency upgrades
must review the lock diff and rerun checks; prereleases are disallowed.

CI verifies Windows/Ubuntu installation, PostgreSQL migrations, API behavior,
RabbitMQ delivery and isolated wheel imports. Routine provider tests use synthetic
or sanitized captured responses. Local Windows checks do not prove native Ubuntu,
live-provider accuracy or inbox delivery.

Additional checks:

| Command or opt-in | Boundary |
| --- | --- |
| `npm --prefix web run check:coverage` | Production-browser V8 coverage of authored TypeScript; writes `web/coverage/lcov.info` |
| `PYRACAST_BROWSER_TEST=1` with `uv run --locked pytest tests/test_browser.py -n 1 --no-cov` | Browser ? real Django/PostgreSQL, fixture providers/mail |
| `PYRACAST_LIVE_QUEUE=1` and dedicated `PYRACAST_RABBITMQ_URL` | Real-broker tests; uses default/calibration queues |
| `uv run --no-project --python 3.15.2 python scripts/check_scientific.py` | Locked optional scientific stack with checksum-pinned HDF5 build |

The scientific helper needs a C compiler. It checks installation, linked HDF5 and a
pvlib calculation, not irradiance accuracy. Persistent installation of the
`scientific` extra needs [native h5py setup](https://docs.h5py.org/en/stable/build.html);
Windows also needs an open `os.add_dll_directory` handle before importing h5py/pvlib.
The default application does not require this extra.

Regenerate the API schema and types after API changes:

```text
npm --prefix web run api:generate
```

This uses production defaults without reading local application settings or credentials.
Commit both generated files; CI runs the same command and checks for differences.

### Dependency and static analysis

```text
uv export --locked --all-groups --all-extras --no-emit-project --no-hashes --output-file audit-requirements.txt
uv run --locked pip-audit --strict --no-deps --disable-pip --requirement audit-requirements.txt
```

Dependabot proposes dependency/image/action updates; no auto-merge is configured.
CI pins workflow actions to immutable commits.

[SonarQube Cloud](https://sonarcloud.io/project/overview?id=erkurtharun_pyracast)
requires automatic analysis disabled and these GitHub settings:

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `SONAR_TOKEN` | Project analysis token |
| Variable | `SONAR_ORGANIZATION` | `erkurtharun` |
| Variable | `SONAR_PROJECT_KEY` | `erkurtharun_pyracast` |
| Variable | `SONAR_ENABLED` | `true` after configuration |

The quality gate consumes backend `coverage.xml` and frontend LCOV. Fork,
Dependabot and merge-group runs do not receive an analysis token. The scanner
targets Python 3.15; analyzer support does not replace the executable checks.

### Authorized live checks

Configure the approved Solcast binding in the admin first:

```text
uv run --locked python scripts/check_solcast.py --live --binding-id BINDING_ID --kind settings
uv run --locked python scripts/check_solcast.py --live --binding-id BINDING_ID --kind forecast
```

Each kind is allowed once per binding/UTC day and reserves shared account quota before HTTP,
without automatic retries. Sanitized captures go to ignored `.codex/solcast/`.
Other applications' quota use remains unknown; coordinate consumers. Never create a
second account record to reset the budget or infer AC/DC semantics from output.

EP?A? checks require `PYRACAST_EPIAS_LIVE=1`, `EPIAS_LIVE_PLANT_ID` and
`EPIAS_LIVE_EIC`, then `uv run --locked pytest tests/test_epias_live.py --no-cov`.
The captured [forecast](tests/fixtures/solcast_forecasts.json) and
[estimated-actuals](tests/fixtures/solcast_estimated_actuals.json) fixtures preserve
integration evidence, not matched irradiance labels. Large historical downloads
and generated case artifacts stay local and ignored.

## Deployment and contributor tools

Production setup, HTTPS, secrets, backups and delivery commands live in the
[deployment guide](docs/DEPLOYMENT.md). The initial deployment runs the web, API,
workers, broker and PostgreSQL on one 4 GiB Compute Engine VM. Hourly off-host
database backups target a one-hour recovery point; VM downtime is accepted.
Deployment remains disabled until the operator configures the project and sets
`GCP_DEPLOY_ENABLED=true`.

[AGENTS.md](AGENTS.md) owns the editing/verification workflow. Shared hooks in
`.codex/hooks.json` format edited Python files and guard source reads; run the full
check explicitly before delivery. Non-managed hooks need the user's `/hooks`
trust, and local MCP configuration/credentials remain ignored.

`ctx7` and `code-review-graph` are user-level CLI tools. Keep `CRG_HOME` and its
graph data outside the checkout. First build the graph with
`code-review-graph build --repo . --skip-flows --data-dir <CRG_HOME>`; updates use
the same directory, while queries resolve it through `CRG_HOME` without a
`--data-dir` flag. The optional [Sonar MCP server](https://github.com/SonarSource/sonarqube-mcp-server)
uses ignored local configuration and Java 21+ or Docker.
