# Application architecture

The structural decisions the system rests on, and why. How a single flow works is on its page
under [flows/](flows/README.md); tables are in [DATA_MODEL.md](DATA_MODEL.md), versions in
[STACK.md](STACK.md), calculations in [IRRADIANCE.md](IRRADIANCE.md) and the production host in
[DEPLOYMENT.md](DEPLOYMENT.md).

## Outcome

Companies submit solar plants with their location and technical declaration; a platform
administrator links an authorized Solcast account and approves the plant. Scheduled jobs then
collect irradiance, publish daily energy estimates and calibrate them against production. Customers
read saved results in a Next.js application; operators work in a Django console. Only provider
accounts authorized for this service are used; a fixture provider lets development run without
credentials or live calls.

## Processes and portability

| Process | Role |
| --- | --- |
| `api` | Django and DRF behind one HTTP origin, with the operator console at `/admin/` |
| `web` | Next.js server for the public site and the customer application |
| `dispatcher` | One loop that creates due job rows in PostgreSQL and publishes them to RabbitMQ |
| `worker` | Dramatiq consumer of the `default`, `calibration` and `epias` queues |
| `notifications` | Dramatiq consumer of the `notifications` queue (reminders and account mail) |
| `migrate` | Release command that applies migrations under its own database role |

PostgreSQL is the only durable state: jobs, leases, retries, results and sessions live there, and
RabbitMQ only wakes a worker: a message carries a row id, a provider-account revalidation also its
lease time (`orchestration/kinds.py`). No process keeps authoritative workflow state in memory, so
any process can be restarted. Mail has its own worker process because a queue alone reserves no
execution capacity: provider work must never delay a sign-in code.
See [worker-runtime](flows/worker-runtime.md) and [dispatcher-cycle](flows/dispatcher-cycle.md).

The API, workers and dispatcher run on Windows x64 and Ubuntu x64, and CI tests both. Correctness
does not depend on Bash, cron, Unix signals beyond `SIGINT`/`SIGTERM`, `fork` or reload features:
entry points are Python, paths use `pathlib`, text is UTF-8 and time is timezone-aware with an
explicit `tzdata`. A stop file gives the local launcher the same graceful shutdown on both systems
([local-dev-stack](flows/local-dev-stack.md)).

Configuration follows [Twelve-Factor](https://12factor.net/config): deployment settings and shared
credentials (application key, database, broker, mail, EPİAŞ login) come from environment
variables; the ignored `.env` is a local convenience that never overrides a set variable
(`environment.py`). Per-plant provider accounts, encrypted API keys and site identifiers are
administrator-managed database records. Logs go to standard error for the supervisor to collect,
because standard output belongs to what a command prints (such as the health check's JSON); the
security log writes to standard output or to the file `PYRACAST_SECURITY_LOG_PATH` names, and
gunicorn's access log to standard output. `PYRACAST_LOG_LEVEL` sets verbosity, and
`DisallowedHost` lines are discarded. See [secret-provisioning](flows/secret-provisioning.md).

## Module boundaries

Each Django app owns the models and migrations of one domain; the table names its main records.

| Package | Owns |
| --- | --- |
| `accounts` | Tenants, memberships, invitations, language preferences and the authentication cache; it writes its mail intents |
| `regions` | Locations (coordinates, timezone, approval, trash), location change requests and the audit log |
| `connections` | Canonical provider accounts with their encrypted keys and quota, and site bindings |
| `forecasts` | Collection schedules and jobs, the request ledger, immutable snapshots, results and reference imports |
| `plants` | Plants, submissions, configuration revisions, production observations, calibration and forecast issuances |
| `incidents` | Outages and their immutable revisions |
| `change_requests` | Member proposals and their decisions |
| `metering` | EPİAŞ connections, collection jobs and snapshots |
| `notifications` | Reminder schedules, recipients, deliveries and outbound mail (`OutboundMail`, declared in `accounts/models.py`) |
| `orchestration` | Dispatcher, worker command, job kinds and the health check (no tables) |
| `api`, `platform_admin` | HTTP surfaces: DRF views and the console views |
| `irradiance`, `solar`, `intervals` | Calculations, free of Django imports |
| `calculation` | The calculation of one site binding's provider data, on top of them |

API views, console views, management commands and job actors call the same application operations,
so a rule is enforced once whatever the entry point. Views validate input, check access and
serialize; serializers do not write; no model signal carries business logic. Authentication
signals in `accounts` feed the security log and session bookkeeping, and a password reset also
removes the sign-in provider identities of an account that never signed in
(`accounts/social.py`). HTTP, persistence and queue details stay out of the calculations, which
receive typed values with units and missingness preserved.

## Tenancy and authorization

A tenant is a workspace. Access derives from an active membership of the tenant the URL names;
tenant ownership is never a writable request field. A workspace has one `admin` (a partial unique
constraint allows no second) and any number of `viewer` members; viewers read and propose, the
administrator decides and edits directly. A platform administrator is an active staff superuser;
that status grants nothing on `/api/tenants/` routes and acts on workspaces only through the
console.

Authorization is checked at three places, so that no single omission opens data:

1. `api/permissions.py` declares the access level and throttle scope of every API route and
   method; a route or method missing from the table is refused.
2. Views and querysets scope every read to the URL tenant.
3. Operations check the role again through `accounts/authorization.py`. A writer locks the tenant
   row first and rereads its membership under that lock, so a reassignment or removal cannot
   interleave with a write; reads take no tenant lock. Only console views pass `platform=True`,
   which replaces the membership with a platform-administrator check.

Lock order: a transaction that locks a person's account (`auth_user`, `FOR NO KEY UPDATE`)
locks it before any workspace row; workspace rows follow as tenant, region, plant, then request
rows. One order, so a person/workspace lock inversion cannot deadlock, and a writer that only
references the person does not wait. `tests/test_lock_order.py` checks it for the operations it
lists; flow pages name their own sequence and link here. See [tenant-authorization](flows/tenant-authorization.md).

## Authentication and sessions

django-allauth's headless browser API is the only authentication authority: no token endpoints
and no second login path. Passwords are hashed with Argon2 and need 15 characters; social sign-in
(Google, Microsoft) never merges or grants access by matching email. The console requires a TOTP
factor, MFA evidence in the session and, for every write, a reauthentication by that authenticator
within the five-minute window. Provider keys, MFA secrets and sealed mail codes are encrypted with
Fernet under HKDF keys derived from the application key, one context per purpose
(`encryption.py`); this protects against a database-only disclosure, not a compromised host
([encryption-key-rotation](flows/encryption-key-rotation.md)).

Sessions are server-side rows. Requests that share a session serialize on a PostgreSQL advisory
lock, so concurrent requests cannot overwrite each other's security state; that requires one
PostgreSQL session per request, so transaction pooling and Django's connection pool are refused.
Sessions end after 12 hours or 30 minutes without a request; any authenticated request, a
background poll included, counts as activity (`SessionLifetime`). Rate limits for authentication
live in a PostgreSQL cache on its own autocommit connection, so a rolled-back request cannot reset
them. See [session-and-csrf](flows/session-and-csrf.md),
[rate-limiting](flows/rate-limiting.md) and the identity flows in the
[flow map](flows/README.md#identity-and-access).

## Background work and its guarantees

- **Durable first.** The dispatcher writes a job row before it publishes, so a commit cannot lose
  work; a failed publish leaves the row ready for the next cycle.
- **PostgreSQL owns retries.** Dramatiq actors use `max_retries=0`; a job's attempts, backoff and
  five-minute lease are columns. Redelivery is assumed: a job runs only under its lease, and
  results are published idempotently.
- **No lock across the network.** Provider and mail calls run outside database transactions; a
  collection rechecks under lock that it still holds its lease and that its binding is still
  current before it publishes.
- **Bounded waits.** Socket, database and broker timeouts are I/O limits; leases and Dramatiq time
  limits bound the job. Both are defined once in `background/deadlines.py`.
- **Quota is per provider account.** Every provider request, including validation and retries,
  reserves a slot on the canonical account's UTC-day ledger first; an uncertain outcome counts as
  used, and accounts are never rotated to evade a limit
  ([provider-quota-and-backoff](flows/provider-quota-and-backoff.md)).
- **Mail is sent at most once.** A send records a transmit marker before it hands the message to
  the mail service; a send interrupted after the marker ends `uncertain` and is never retried
  automatically ([email-transport](flows/email-transport.md)).
- **Failures are logged without messages.** `background.failures.log_failure` records an event,
  fields, the error type and code location, never the exception text, which can carry provider
  URLs or keys. Security events that report a change are logged when their transaction commits
  ([audit-trail](flows/audit-trail.md)).

[delivery-health-check](flows/delivery-health-check.md) turns these records into one health verdict.

## Data ownership and provenance

- Acquisitions are immutable: each provider response, import and EPİAŞ read is a new snapshot
  with its payload and acquisition time. Corrections of production values and outages append
  revisions instead of rewriting the row.
- Plant configurations are immutable revisions; a forecast issuance freezes the configuration,
  calibration and outage knowledge of its publication, so later changes never alter an archived
  forecast.
- Approval (`pending`, `approved`, `rejected`, `suspended`), connection health and data freshness
  are separate facts; approval does not mean a forecast exists
  ([plant-approval](flows/plant-approval.md)).
- Times are `timestamptz` in UTC with each region's IANA timezone; quantities carry their unit in
  the name; unknown values stay null and are never zero.
- Customers see saved records only: the frontend never calls a provider, and the customer JSON
  renderer (`api/public.py`) removes provider names, account and site identities, keys and raw
  payloads from every response and passes stored provenance only through allowlists; stored rows
  keep their full provenance ([provider-detail-redaction](flows/provider-detail-redaction.md)).
- A trashed location hides its plant and everything under it; queries select live rows through
  `Region.objects.active()` and `Plant.objects.active()`. Data is kept for 30 days, then purged
  ([plant-trash-and-restore](flows/plant-trash-and-restore.md),
  [trash-retention-purge](flows/trash-retention-purge.md)).

## Database invariants and rollout

Django migrations are the only schema owner; Flyway, Liquibase and manual DDL are not used.
Applied migrations are immutable, and fixes are new forward migrations. The project `migrate`
command holds a session advisory lock so concurrent runners fail fast, needs a direct connection,
and refuses a database whose ledger names a project migration that is not on disk
([database-migration](flows/database-migration.md)).

The database enforces what must never be violated, beside the locked operations: partial unique
constraints (one plant per location, one active location per coordinate pair within a tenant),
GiST exclusion constraints (non-overlapping observation and outage periods), deferred composite
foreign keys (both paths of an ownership relation agree), row triggers (rows that must not change
once written or published) and CHECK constraints (ranges, finite values, closed vocabularies).
Objects models cannot declare are listed once in `schema_objects.py`, read by their migrations
and the schema catalog test. Constraint and index names follow `<model>_<subject>_<kind>` with `chk`, `uniq`, `excl`, `fk` or
`idx` (`tests/test_schema_names.py`). These are not row-level security: access and publication
rules stay in the operations, and direct SQL must not create business records.

Project tables keep `bigint` identity keys (`DEFAULT_AUTO_FIELD`): replacing them with UUIDv7 has
no demonstrated need. JSONB holds provider payloads and versioned numerical artifacts; foreign
keys, scheduling selectors and quantities that need database rules stay typed columns, so the
database enforces them.

Provider account identity is PostgreSQL `casefold()` under the `pg_unicode_fast` collation, used
by the unique index, lookups and inserts alike, so one account cannot gain a second quota budget
through spelling.

Before a release, review `migrate --plan` and `sqlmigrate`, and test against an empty database and
a restored copy. On a large table, build each concurrent index in its own non-atomic migration and
add, backfill, switch and validate a column before removing the old one. A reverse migration can
discard data, so recovery is a forward repair. Backups and restores are in
[backup-and-restore-rehearsal](flows/backup-and-restore-rehearsal.md) and
[DEPLOYMENT.md](DEPLOYMENT.md#operations-and-recovery).

## Web application

One origin serves the Next.js pages, `/api/`, `/admin/` and Django's `/static/`: the production
gateway routes them ([edge-gateway-and-tls](flows/edge-gateway-and-tls.md)), the Next.js
development server proxies them. One origin lets the session cookie stay `HttpOnly`,
`SameSite=Lax` and, in production, `Secure` with a `__Host-` prefix; Django owns authentication;
allauth's browser API checks CSRF also before sign-in, and DRF on every signed-in request
([session-and-csrf](flows/session-and-csrf.md)).

`web/src/app` holds the routes: public pages that render without a session, and the private
application under `/app/`. `web/src/features` gives each workflow one folder that owns its state;
`web/src/lib` holds the API client, typed by `schema.d.ts` generated from the backend OpenAPI
schema at build time ([api-schema-and-client-types](flows/api-schema-and-client-types.md));
`web/src/ui` holds shared primitives. Server data flows through TanStack Query
([frontend-data-layer](flows/frontend-data-layer.md)). Every Next.js page carries a nonce-based
CSP and `private, no-store` (`web/src/proxy.ts`); `noindex` on private pages is a crawler hint,
not access control ([security-headers-and-proxy-trust](flows/security-headers-and-proxy-trust.md)).

The interface is English and Turkish on shared URLs: next-intl localizes the web application,
Django gettext the console, validation and mail. Plant timezones, units, timestamps and CSV values
do not change with the language ([i18n-and-language](flows/i18n-and-language.md)).

## API contract

The customer API exposes only what the web application calls; provider setup, validation and
approval live in the console. Endpoints accept JSON only, paginate record lists (a workspace's
members and open invitations come whole) and bound date ranges. Every `/api/` failure answers one
localized body `{code, message, fields?, retry_after?, request_id?}` whose codes are a closed
vocabulary exported to OpenAPI; allauth's views carry the same keys beside their own. State
transitions such as approval are operations, never writable status fields. User text says
"outage"; `incident` stays the internal name. See
[api-error-contract](flows/api-error-contract.md).

## Primary references

- [The Twelve-Factor App](https://12factor.net/), [Django design philosophies](https://docs.djangoproject.com/en/6.1/misc/design-philosophies/), [OWASP Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html).
- [Django migrations](https://docs.djangoproject.com/en/6.1/topics/migrations/), [staged constraints and concurrent indexes](https://docs.djangoproject.com/en/6.1/ref/contrib/postgres/operations/), [PostgreSQL constraint timing](https://www.postgresql.org/docs/current/sql-set-constraints.html), [PostgreSQL backup and recovery](https://www.postgresql.org/docs/18/backup.html).
- [django-allauth headless](https://docs.allauth.org/en/latest/headless/), [NIST SP 800-63B-4](https://pages.nist.gov/800-63-4/sp800-63b.html), [OWASP authentication guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html), [OAuth security best current practice](https://www.rfc-editor.org/rfc/rfc9700.html).
- [Dramatiq retries and time limits](https://dramatiq.io/guide.html), [HTTPX timeouts](https://www.python-httpx.org/advanced/timeouts/), [AWS retries and jitter](https://builder.aws.com/content/3EumjoZascWd1oZiEgL8ORlv3qE/timeouts-retries-and-backoff-with-jitter).
