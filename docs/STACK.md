# Stack reference

Pyracast is a Django API with a Next.js single-page client, one PostgreSQL database that owns all durable state (including job leases and retries), RabbitMQ-woken workers, and digest-pinned containers on a single Compute Engine VM behind Cloudflare. This page gives each part of that stack at its resolved version and the few externally sourced rules that bind work on it here. System design lives in [ARCHITECTURE.md](ARCHITECTURE.md), calculations in [IRRADIANCE.md](IRRADIANCE.md) and operations in [DEPLOYMENT.md](DEPLOYMENT.md). Versions and sources were read on 2026-11-17.

## Stack at a glance

| Part | Technology and resolved version | Role in this system | Configured in |
| --- | --- | --- | --- |
| Python runtime and tooling | CPython 3.15.2; uv 0.13.7 (CI, local) and 0.13.8 (image build); ruff 0.17.5; mypy 2.4.1 with django-stubs 6.1.2; pytest 9.2.0, pytest-django 4.14.0, pytest-xdist 3.8.0, pytest-cov 7.1.0, coverage 7.17.2; pip-audit 2.10.1 | Interpreter, lockfile, lint, strict typing, tests with a 100 % branch-coverage floor, dependency audit | `pyproject.toml`, `uv.lock`, `.python-version`, `scripts/check.py` |
| Django and DRF | Django 6.1.3; djangorestframework 3.18.3; drf-spectacular 0.30.0; WhiteNoise 6.12.0; gunicorn 27.0.1 | Session-authenticated JSON API, platform admin, generated OpenAPI contract, static files | `src/pyracast/settings.py`, `src/pyracast/production.py`, `src/pyracast/urls.py`, `deploy/gunicorn_conf.py` |
| Authentication and security | django-allauth 65.22.1 (headless, mfa, socialaccount); argon2-cffi 25.1.0; cryptography 50.0.2 | Browser sessions, email codes, TOTP, Google and Microsoft sign-in, encrypted provider keys and MFA seeds | `src/pyracast/settings.py`, `src/pyracast/accounts/`, `src/pyracast/encryption.py` |
| PostgreSQL | PostgreSQL 18.7 (production image) and 18.6 (development, CI tests); psycopg 3.3.9 | Only relational store: tenant data, job leases and retries, sessions, rate-limit cache | `src/pyracast/settings.py`, `deploy/postgres/Dockerfile`, `deploy/bootstrap.sql`, `scripts/start_postgres.py` |
| Background jobs | Dramatiq 2.2.1; pika 1.4.4; RabbitMQ 4.4.0 | Messages carry row IDs to four actors; a dispatcher loop re-derives due work from PostgreSQL | `src/pyracast/runtime.py`, `src/pyracast/forecasts/management/commands/`, `deploy/compose.yaml`, `scripts/start_rabbitmq.py` |
| External providers and email | httpx 0.28.1; openpyxl 3.1.5; Django `MAILERS` (SMTP or Gmail API) | Solcast, Open-Meteo and EPIAS clients; reminder and account mail; XLSX attachments | `src/pyracast/providers.py`, `weather.py`, `epias.py`, `smtp.py`, `gmail.py`, `provider_retry.py` |
| Solar and numerical core | Pure Python; astral 3.2; tzdata 2026.6; pvlib 0.16.1 (optional `scientific` extra, never in the runtime image); hypothesis 6.170.15 | Daylight windows, irradiance integration, calibration fits, tracker gain | `src/pyracast/solar.py`, `src/pyracast/irradiance/`, `src/pyracast/plants/numerics.py`, `docs/IRRADIANCE.md` |
| Frontend | Next.js 16.4.6; React 19.3.1; next-intl 4.15.1; TypeScript 5.9.3; ESLint 10.14.0; openapi-typescript 7.13.0; Node.js 26.13.1 (image and CI; 26.13.0 is the local floor) | Client-rendered workspace application and server-rendered public pages | `web/package.json`, `web/next.config.ts`, `web/src/proxy.ts`, `web/tsconfig.json`, `web/eslint.config.mjs` |
| Frontend and end-to-end testing | @playwright/test 1.65.0 (Chromium); @axe-core/playwright 4.14.0; monocart-coverage-reports 3.2.1 | Mocked-API browser suite, real-backend suite driven by pytest, accessibility scans, browser coverage | `web/playwright.config.ts`, `web/playwright.integration.config.ts`, `web/testing/`, `tests/test_browser.py` |
| Build, deployment and operations | Docker Engine 29.9.3 and Compose 5.7.1 (VM); Traefik 3.7.19, built from source with Go 1.27.4; Terraform 1.17.2 with google provider 8.11.0; Trivy 0.76.2; GitHub Actions pinned by SHA | Build once in CI, promote the same image digests to one VM, hourly off-host backups | `Dockerfile`, `web/Dockerfile`, `deploy/`, `infra/terraform/`, `.github/workflows/` |
| Code scanning | Qodana CLI 2026.2.3 (local) with two configurations: `qodana.yaml` (`qodana-python`, everything outside `web/`) and `qodana-js.yaml` (`qodana-js`, `web/`); neither pins a linter build; SonarQube Cloud through scan action 8.5.0 (CI) | Static analysis and the release quality gate | `qodana.yaml`, `qodana-js.yaml`, `sonar-project.properties`, `.github/workflows/ci.yml` |

## Python runtime and tooling

- `uv sync --locked` and `uv run --locked` fail on a stale `uv.lock`, plain `uv run` re-locks and syncs first, and `uv lock --check` is the standalone assertion. [uv: locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/)
- Declare `uv_build` as a range with an upper bound: uv uses its bundled backend only when its own version satisfies the requirement and otherwise falls back to a `uv_build` package that `uv.lock` does not cover. [uv: build backend](https://docs.astral.sh/uv/concepts/build-backend/)
- An explicit `lint.select` replaces Ruff's default rule set while `extend-select` adds to it, and the docs prefer the explicit form, so every rule family is a deliberate choice. [Ruff linter](https://docs.astral.sh/ruff/linter/)
- Silence missing imports per module, and only for libraries that ship neither stubs nor `py.typed`; an override is equivalent to a `type: ignore` on every import of that module. [mypy: running mypy](https://mypy.readthedocs.io/en/stable/running_mypy.html)
- django-stubs constrains mypy only through its `compatible-mypy` extra (6.1.2 allows `mypy>=1.13,<2.5`), and its plugin needs the configured settings module to import cleanly. [django-stubs](https://github.com/typeddjango/django-stubs)
- pytest 9 reads a native `[tool.pytest]` table whose `strict = true` enables strict config, markers, parametrization ids and xfail; it cannot be combined with `[tool.pytest.ini_options]`. [pytest configuration](https://docs.pytest.org/en/stable/reference/customize.html)
- Audit a fully pinned export with `pip-audit --strict --no-deps --disable-pip`; pip-audit documents `pylock.*.toml` input but not `uv.lock`. [pip-audit](https://github.com/pypa/pip-audit)

## Django and DRF

- Django supports Python 3.15 only from 6.1.3, so the `>=6.1.3` floor must not be lowered. [Django 6.1.3 release notes](https://docs.djangoproject.com/en/6.1/releases/6.1.3/)
- Use `CSP.NONCE` with the `csp` context processor, put `{% csp_nonce_attr %}` on external script and link tags, and never serve nonce-bearing HTML from a cache. [Django CSP how-to](https://docs.djangoproject.com/en/6.1/howto/csp/)
- With `DEBUG` off, Django's default logging sends `django.request` 5xx tracebacks only to `mail_admins`, so production needs an explicit handler for `django` or `django.request`. [Django default logging](https://docs.djangoproject.com/en/6.1/ref/logging/#default-logging-configuration)
- DRF parses JSON, form and multipart bodies by default; set `DEFAULT_PARSER_CLASSES` to exactly what the API accepts. [DRF settings](https://www.django-rest-framework.org/api-guide/settings/)
- DRF throttles are scoped per view and need `NUM_PROXIES` behind proxies, but they "should not be considered a security measure" against brute force or denial of service. [DRF throttling](https://www.django-rest-framework.org/api-guide/throttling/)
- A custom exception handler calls DRF's default handler and adjusts its response; returning `None` produces a 500. The default handler serializes only `detail`, so a domain error's code (`APIException.default_code`) reaches the JSON body only if the custom handler reads it, for example with `exc.get_codes()`. [DRF exceptions](https://www.django-rest-framework.org/api-guide/exceptions/)
- An OpenAPI operation is expected to document its success response and every known error; declare them with `@extend_schema(responses={status: serializer})`. [drf-spectacular customization](https://drf-spectacular.readthedocs.io/en/latest/customization.html)

## Authentication and security

- allauth rate limits need a cache shared by all workers with an atomic `cache.add()` and strongly consistent reads, plus a trusted client IP setting because `X-Forwarded-For` is distrusted by default. [allauth rate limits](https://docs.allauth.org/en/latest/common/rate_limits.html)
- Django rotates the CSRF secret on every login, so a client holding a pre-login token must fetch a new one. [Django CSRF](https://docs.djangoproject.com/en/6.1/ref/csrf/)
- Django never purges expired database sessions; `clearsessions` has to run on a schedule. [Django sessions](https://docs.djangoproject.com/en/6.1/topics/http/sessions/)
- A password used as the only factor SHALL be at least 15 characters (8 only when always combined with MFA), SHOULD be allowed to reach at least 64, and SHALL be compared against a blocklist of common and compromised values. [NIST SP 800-63B-4](https://pages.nist.gov/800-63-4/sp800-63b.html)
- An AAL2 verifier SHALL offer a phishing-resistant option, email SHALL NOT serve as an out-of-band authenticator, and saved recovery codes SHALL hold at least 64 bits and be stored hashed. [NIST SP 800-63B-4](https://pages.nist.gov/800-63-4/sp800-63b.html)
- ASVS 5.0 asks for a new session token on re-authentication (7.2.4), full re-authentication before changing email, MFA or recovery data (7.5.1), and a way for users to view and end their sessions (7.5.2). [OWASP ASVS 5.0 V7](https://raw.githubusercontent.com/OWASP/ASVS/v5.0.0/5.0/en/0x16-V7-Session-Management.md)
- Deny by default, validate permissions on every request and prefer application-wide configuration of the checks. [OWASP Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)

## PostgreSQL

- Persistent connections (`CONN_MAX_AGE`) need `CONN_HEALTH_CHECKS=True` to survive a server restart, every thread holds its own connection per alias, and psycopg pooling requires `CONN_MAX_AGE=0`. [Django databases](https://docs.djangoproject.com/en/6.1/ref/databases/)
- Set `statement_timeout` and `lock_timeout` per session or role rather than in `postgresql.conf`, keep `lock_timeout` below `statement_timeout`, and use `idle_in_transaction_session_timeout` so a stalled transaction cannot hold locks and block vacuum. [PostgreSQL client connection defaults](https://www.postgresql.org/docs/18/runtime-config-client.html)
- Acquire row locks in one consistent order and retry deadlock aborts where that order cannot be proven; session-level advisory locks survive rollback and need one unlock per lock request. [PostgreSQL explicit locking](https://www.postgresql.org/docs/18/explicit-locking.html)
- Add constraints `NOT VALID` and validate them in a separate migration (Django's operations cover CHECK only; foreign keys need `RunSQL`), and build indexes with `AddIndexConcurrently` in a non-atomic migration. [Django PostgreSQL migration operations](https://docs.djangoproject.com/en/6.1/ref/contrib/postgres/operations/)
- `lower()`, `upper()` and sort order follow the database locale, which is fixed at creation and can behave differently per platform under the libc provider; ICU is independent of the operating system and the `builtin` provider offers `PG_UNICODE_FAST`. [PostgreSQL locale support](https://www.postgresql.org/docs/18/locale.html)
- `pg_dump` covers one database without roles or tablespaces, cannot give point-in-time recovery, and leaves out optimizer statistics by default, so a restore still needs `ANALYZE`. [pg_dump](https://www.postgresql.org/docs/18/app-pgdump.html)
- Run the current minor release in every environment; moving within 18.x needs no dump and restore. [PostgreSQL versioning policy](https://www.postgresql.org/support/versioning/)

## Background jobs

- Actors must be idempotent and take small, JSON-simple arguments such as row IDs, because the same message can be delivered more than once. [Dramatiq best practices](https://dramatiq.io/best_practices.html)
- Dramatiq acknowledges after processing and dead-letters failures to `xq_*` queues with a seven-day TTL; with `max_retries=0` the first exception does that, so a durable owner outside the broker must retry. [Dramatiq advanced topics](https://dramatiq.io/advanced.html)
- `TimeLimit` starts its timer only on the `process_boot` event, which the `dramatiq` CLI emits and a hand-built `Worker` must emit itself; the limit is best-effort and cannot interrupt system calls. [Dramatiq 2.2.1 TimeLimit source](https://github.com/Bogdanp/dramatiq/blob/v2.2.1/dramatiq/middleware/time_limit.py)
- `Worker.stop()` waits up to 600 s for in-flight messages and requeues prefetched ones, so the container stop grace period must exceed that timeout. [Dramatiq advanced topics](https://dramatiq.io/advanced.html)
- Bind worker groups to queues so important work is not stuck behind slow actors; a worker given no queues consumes every declared queue. [Dramatiq cookbook](https://dramatiq.io/cookbook.html)
- Delete or restrict the `guest` user in production and monitor the broker itself (memory watermark, disk free limit, file descriptors). [RabbitMQ production checklist](https://www.rabbitmq.com/docs/production-checklist)
- The default-user environment variables create a user only on the first boot of a blank node; they do not change credentials on an existing data volume. [RabbitMQ access control](https://www.rabbitmq.com/docs/access-control)

## External providers and email

- Retry only transient failures, at a single layer, with capped backoff, bounded attempts and jitter; never auto-retry a side-effecting call that offers no idempotency. [Timeouts, retries and backoff with jitter](https://builder.aws.com/content/3EumjoZascWd1oZiEgL8ORlv3qE/timeouts-retries-and-backoff-with-jitter)
- Solcast: retry 429 and 5xx with exponential backoff and jitter and never other 4xx; 402 means the plan allowance is used up, and `Retry-After` is documented for 429 only. [Solcast errors](https://docs.solcast.com.au/docs/errors)
- Open-Meteo's free API is for non-commercial use only; a subscription product has to call the `customer-` host with an `apikey`, and the data is licensed CC BY 4.0. [Open-Meteo terms](https://open-meteo.com/en/terms)
- Django 6.1 configures mail through `MAILERS`; a custom backend consumes its own keyword arguments before `super().__init__()` and keeps its own `fail_silently` attribute. [Django 6.1 release notes](https://docs.djangoproject.com/en/6.1/releases/6.1/)
- `transaction.on_commit` callbacks run after the commit and outside the transaction: a failing callback cannot roll anything back, and without `robust=True` it also stops later callbacks. [Django transactions](https://docs.djangoproject.com/en/6.1/topics/db/transactions/)
- An RFC 2047 encoded-word must never appear in a `Content-Type` or `Content-Disposition` parameter, so mail tests assert the serialized header, not the re-parsed message. [RFC 2047 section 5](https://www.rfc-editor.org/rfc/rfc2047.html)
- Spreadsheet cells that start with `=`, `+`, `-`, `@`, a tab or a line break can be read as formulas; in XLSX the defence is forcing every text cell to the string type. [OWASP CSV Injection](https://owasp.org/www-community/attacks/CSV_Injection)

## Solar and numerical core

- Subtraction between datetimes in the same zone ignores `fold`, so compute durations in UTC and use zone arithmetic only for calendar-day bounds. [PEP 495](https://peps.python.org/pep-0495/)
- Declare `tzdata` as a dependency for cross-platform projects, because Windows ships no system IANA database. [zoneinfo](https://docs.python.org/3.15/library/zoneinfo.html)
- astral 3.2 (2022-11-05) is its latest release; it installs on 3.15 (`Requires-Python >=3.7,<4.0`) but its classifiers stop at 3.10, so its behaviour on 3.15 is proven solely by this repository's tests. [astral on PyPI](https://pypi.org/pypi/astral/json)
- Python 3.14 added detailed `math` domain-error messages, so never match on exception text; the same release allows `except A, B:` without parentheses only when there is no `as` clause. [What's new in Python 3.14](https://docs.python.org/3.14/whatsnew/3.14.html)
- Erbs decomposition takes the true (not refraction-corrected) zenith, while single-axis tracker geometry takes the apparent zenith. [pvlib erbs](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.irradiance.erbs.html)
- Evaluate forecasts with a rolling origin: the training set holds only observations that occurred before the test observation. [Forecasting: Principles and Practice, time series cross-validation](https://otexts.com/fpp3/tscv.html)
- Load Hypothesis settings profiles by environment (the built-in `ci` profile derandomizes and removes the deadline) and pin regressions with `@example`, because the example database is only a cache. [Hypothesis API reference](https://hypothesis.readthedocs.io/en/latest/reference/api.html)

## Frontend

- A nonce-based CSP is set in `proxy.ts` on both the request and the response. Next.js applies the nonce only while rendering a request, so every page that carries it must render dynamically (a page without a request API opts in with `await connection()`): no static optimisation, ISR, PPR or CDN caching. [Next.js CSP guide](https://nextjs.org/docs/app/guides/content-security-policy)
- Uncaught render errors need `error.tsx` per segment plus `global-error.tsx` and `not-found.tsx`; error boundaries do not catch event-handler or async errors, and `retry()` is preferred over `reset()`. [Next.js error file convention](https://nextjs.org/docs/app/api-reference/file-conventions/error)
- Client Components that need a shared browser cache, focus revalidation, polling or request deduplication use SWR or TanStack Query. [Next.js client-side data fetching](https://nextjs.org/docs/app/guides/client-side-data-fetching)
- Fetching inside an Effect needs cleanup against races; derive values during render or reset state with `key` instead of syncing it in Effects. [You Might Not Need an Effect](https://react.dev/learn/you-might-not-need-an-effect)
- `fetch()` has no timeout option; bound every request with `signal: AbortSignal.timeout(ms)`. [MDN AbortSignal.timeout](https://developer.mozilla.org/en-US/docs/Web/API/AbortSignal/timeout_static)
- A `NextIntlClientProvider` rendered from a Server Component inherits all messages into the client payload; pass a subset or `messages={null}`, and note that `messages` on a nested provider replace the inherited ones. [next-intl configuration](https://next-intl.dev/docs/usage/configuration)
- openapi-typescript calls `noUncheckedIndexedAccess` "highly recommended", and its generated `paths` can type a path-aware client such as openapi-fetch. [openapi-typescript introduction](https://openapi-ts.dev/introduction)

## Frontend and end-to-end testing

- Keep every test isolated and independently runnable, use user-facing locators (`getByRole` first) with awaited web-first assertions, and replace third-party servers through the network API instead of calling them. [Playwright best practices](https://playwright.dev/docs/best-practices)
- Enable `@typescript-eslint/no-floating-promises` and run `tsc --noEmit` on CI so a missing `await` cannot pass silently. [Playwright best practices](https://playwright.dev/docs/best-practices)
- On CI prefer `workers: 1`, always set a global timeout and do not cache browser binaries. [Playwright continuous integration](https://playwright.dev/docs/ci)
- `forbidOnly` and `failOnFlakyTests` exist for CI: with retries enabled, a flaky test otherwise keeps the run green. [Playwright TestConfig](https://playwright.dev/docs/api/class-testconfig)
- Next.js recommends running Playwright against production code (`next build` then `next start`); the dev server is only the alternative. [Next.js Playwright guide](https://nextjs.org/docs/app/guides/testing/playwright)
- Target WCAG A and AA with `withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa'])`, share one `AxeBuilder` configuration through a fixture, and remember that automated scans cannot detect every violation. [Playwright accessibility testing](https://playwright.dev/docs/accessibility-testing)
- Status messages must be announced without moving focus: the `role="status"` container exists before the message is inserted into it. [WCAG technique ARIA22](https://www.w3.org/WAI/WCAG22/Techniques/aria/ARIA22)

## Build, deployment and operations

- Build Python images with a digest-pinned uv, `uv sync --locked`, a dependency layer before the project (`--no-install-project`), a cache mount, compiled bytecode and `--no-editable`, then copy only `.venv` into the runtime stage. [uv Docker guide](https://docs.astral.sh/uv/guides/integration/docker/)
- GitHub-to-Google Workload Identity Federation needs an attribute condition that restricts tokens to the organization, written with the numeric `repository_id` and `repository_owner_id` claims because names can be re-registered. [Google Cloud: WIF with deployment pipelines](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)
- Reference Secret Manager secrets by version number, never by the `latest` alias, and enable data-access logs for `AccessSecretVersion`. [Secret Manager best practices](https://docs.cloud.google.com/secret-manager/docs/best-practices)
- Allowlisting Cloudflare IP ranges is only "moderately secure" origin protection; Authenticated Origin Pulls with Full (strict) lets the origin authenticate the edge. [Cloudflare: protect your origin server](https://developers.cloudflare.com/fundamentals/security/protect-your-origin-server/)
- Keep Traefik's `aliasHeadersStrategy` at `delete` or `reject` in front of header-normalizing backends such as WSGI servers. [Traefik header aliases](https://doc.traefik.io/traefik/v3.7/security/header-aliases/)
- Compute Engine startup scripts run as root on every boot, and only once a network is available, so each of their steps is re-executed at every restart. [Compute Engine startup scripts](https://docs.cloud.google.com/compute/docs/instances/startup-scripts/linux)
- Limit Gunicorn's `forwarded_allow_ips` to trusted front-ends, keep `worker_tmp_dir` off disk-backed filesystems, and use a higher `keepalive` behind a load balancer. [Gunicorn settings](https://gunicorn.org/reference/settings/)

## Code scanning

- `qodana.recommended` leaves JavaScript and TypeScript inspections disabled; enable inspections or groups under `profile.inspections` with `enabled: true`. [Qodana inspection profiles](https://www.jetbrains.com/help/qodana/2026.2/inspection-profiles.html)
- A failing `qodana.sanity` inspection means every other result may be wrong; fix its cause instead of passing `--disable-sanity`. [Qodana inspection profiles](https://www.jetbrains.com/help/qodana/2026.2/inspection-profiles.html)
- In native mode (`withinDocker: false`) the CLI's `-e` flag does not apply (CLI 2026.2.3 help: container runs only), so `QODANA_TOKEN` must already be in the process environment. [Qodana YAML reference](https://www.jetbrains.com/help/qodana/2026.2/qodana-yaml.html)
- A monorepo gets one YAML config per linter, one `qodana scan --only-directory <dir> --config <file>` run per config, and one Qodana Cloud project per run. [Qodana monorepo projects](https://www.jetbrains.com/help/qodana/2026.2/monorepo-project.html)
- SonarQube Cloud imports coverage only under CI-based analysis; Python needs Cobertura XML through `sonar.python.coverage.reportPaths`, produced with `relative_files = True`. [Sonar Python test coverage](https://docs.sonarsource.com/sonarqube-cloud/enriching/test-coverage/python-test-coverage/)
- Sonar's JS/TS analyzer covers only files under `sonar.sources`, ignores `.d.ts` files and walks the tree for tsconfig files unless `sonar.typescript.tsconfigPaths` is set. [Sonar JavaScript, TypeScript and CSS](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/languages/javascript-typescript-css/)
- Sonar documents full Python support for versions 3.0 to 3.14 only; `sonar.python.version` takes a comma-separated list of `X.Y` values. [Sonar Python](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/languages/python/)

## Keeping this current

Lockfiles and pins are the source of truth; the table above is a snapshot of them and declared ranges are never authoritative.

- Python packages: the `version` under each `name` in `uv.lock`; `uv lock --check` asserts that it still matches `pyproject.toml`. Interpreter: `.python-version` and `requires-python` in `pyproject.toml`.
- Web packages: `packages["node_modules/<name>"].version` in `web/package-lock.json`. Node.js: `engines` in `web/package.json`, `web/Dockerfile` and the `node-version` inputs in `.github/workflows/ci.yml`.
- Images and services: the tags and digests in `Dockerfile`, `web/Dockerfile`, `deploy/postgres/Dockerfile` and `deploy/compose.yaml`; Traefik in `deploy/gateway/go.mod`; Go in the `golang` base image of `deploy/gateway/Dockerfile` and the `go` directive of `deploy/gateway/go.mod`; development and CI services in `scripts/start_postgres.py` and `scripts/start_rabbitmq.py`.
- Infrastructure and CI: `infra/terraform/versions.tf` with `infra/terraform/.terraform.lock.hcl`, the Docker pins in `infra/terraform/templates/startup.sh.tftpl`, and the tool versions and action SHAs in `.github/workflows/`.
- Scanners: `qodana --version` for the local CLI and the scan action pin in `.github/workflows/ci.yml`.

When a pin changes, update its table row and re-read the linked sources of that part; a rule that names a version or a default may no longer hold.
