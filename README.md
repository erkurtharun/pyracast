# Pyracast

Multi-tenant irradiance forecasts and daily plant energy estimates: a Django API, a Next.js
interface for customers and an MFA-protected operations console at `/admin/`. PostgreSQL holds all
durable state, including jobs and provider quotas; RabbitMQ wakes the Dramatiq workers.

Reconstructed irradiance and plant transfer estimates are model proxies. Fixture tests and
successful provider requests do not establish measured accuracy.

## Documentation

| Read | For |
| --- | --- |
| [docs/README.md](docs/README.md) | Index of every document |
| [docs/STACK.md](docs/STACK.md) | Versions and the external rules each part relies on |
| [docs/flows/README.md](docs/flows/README.md) | One page per user, operator and background flow |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System-wide design decisions |
| [docs/IRRADIANCE.md](docs/IRRADIANCE.md) | Calculations, calibration and provider contracts |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Production setup, release and recovery |
| [AGENTS.md](AGENTS.md) | Working agreement for contributors and coding agents |

## Set up

Install CPython **3.15.2**, [uv](https://docs.astral.sh/uv/getting-started/installation/) and
Node.js **26.13.0+** with npm. From the repository root, in PowerShell or a Unix shell:

```text
uv sync --locked
npm --prefix web ci --ignore-scripts
```

Copy `.env.example` to `.env` (ignored) and fill it in; processes read it from their working
directory and never override variables already set. Plant-specific Solcast accounts and keys
belong in the operations console, not in `.env`.

The launcher below starts a missing local PostgreSQL itself (pinned binaries on Windows, a pinned
Docker image on Ubuntu), and a missing RabbitMQ on Windows only. To start them by hand, pass the
port `.env` names:

```text
uv run --locked python -m scripts.start_postgres --port 5432
uv run --locked python -m scripts.start_rabbitmq
```

`scripts.start_rabbitmq` is Windows-only. On Ubuntu, run RabbitMQ in Docker:

```text
docker run -d --name pyracast-rabbitmq --hostname pyracast-rabbitmq -v pyracast-rabbitmq-data:/var/lib/rabbitmq -p 127.0.0.1:5672:5672 rabbitmq:4.4.1@sha256:183272aa2ff82718cabeef343de8d9a900931ea65fda4e563fed2a6d9efde578
```

Both helpers are for development only and listen on loopback; the PostgreSQL helper trusts every
loopback connection.

## Run locally

```text
uv run --locked python -m scripts.dev start
uv run --locked python -m scripts.dev status
uv run --locked python -m scripts.dev stop
```

`start` compiles the translation catalogs, migrates the database and starts the API, frontend,
collection worker, mailer and dispatcher; open `http://127.0.0.1:3000`. `stop` leaves PostgreSQL
and RabbitMQ running. On Windows, `start.cmd`, `status.cmd` and `stop.cmd` do the same, and
`start.cmd` also opens the browser unless given `--no-browser`. Processes, logs and failure
messages: [local development stack](docs/flows/local-dev-stack.md).

### First administrator and demo

```text
uv run --locked pyracast bootstrap_admin --email admin@example.com
uv run --locked pyracast fixture_demo --username USERNAME_SHOWN_BY_BOOTSTRAP
```

`bootstrap_admin` asks for a password and shows a private authenticator QR code, setup key and
link; keep them out of logs. `fixture_demo` creates a synthetic plant and its first forecast
without RabbitMQ or provider requests; its output is a fixture, not an accuracy reference. See
[platform administrator bootstrap](docs/flows/platform-admin-bootstrap.md) and
[fixture demo](docs/flows/fixture-demo.md).

## Verification

```text
uv run --locked python scripts/check.py
```

It runs the static stages, the API schema and web static checks (only when `web/node_modules`
exists), then parallel pytest at the coverage floor of `pyproject.toml`. `--static-only` skips
pytest, `--tests-only` runs only pytest, and arguments after `--` go to pytest (add `--no-cov` when
selecting tests). A direct `pytest` call needs `uv run --locked python scripts/translations.py`
first. Stages, skips and CI jobs: [developer quality gate](docs/flows/developer-quality-gate.md).

Further checks:

| Command | Checks |
| --- | --- |
| `npm --prefix web exec -- playwright install chromium`, then `npm --prefix web run check` | Web static checks and the mocked-API browser suite |
| `npm --prefix web run build`, then `uv run --locked pytest tests/e2e -m browser -n 1 --no-cov` | Browser scenarios against real Django and PostgreSQL behind the production build |
| `uv run --no-project --python 3.15.2 python scripts/check_scientific.py` | The optional `scientific` extra (needs a C compiler) and the pvlib recomputation of the tracker gain table |
| `npm --prefix web run api:generate` | Regenerates `web/openapi.json` and its types after an API change; commit both |
| `uv build --no-sources` | Packaging |

Live checks reach real services and run only on request; the Solcast capture spends account quota:

| Command or setting | Checks |
| --- | --- |
| `PYRACAST_LIVE_QUEUE=1` with a dedicated `PYRACAST_RABBITMQ_URL` | Real-broker delivery tests ([worker runtime](docs/flows/worker-runtime.md)) |
| `uv run --locked python scripts/check_solcast.py --live --binding-id BINDING_ID --kind settings` (or `--kind forecast`) | One quota-counted Solcast capture per binding, kind and UTC day ([provider connection setup](docs/flows/provider-connection-setup.md)) |
| `PYRACAST_EPIAS_LIVE=1`, `EPIAS_LIVE_PLANT_ID` and `EPIAS_LIVE_EIC`, then `uv run --locked pytest tests/test_epias_live.py --no-cov` | Authorized EPİAŞ transparency-service access |

Dependency audit:

```text
uv export --locked --all-groups --all-extras --no-emit-project --no-hashes --output-file audit-requirements.txt
uv run --locked pip-audit --strict --no-deps --disable-pip --requirement audit-requirements.txt
```

Scanning and dependency updates: [dependency and code scanning](docs/flows/dependency-and-code-scanning.md).
