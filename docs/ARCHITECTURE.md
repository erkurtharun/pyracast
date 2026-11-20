# Application architecture

## Outcome

Companies submit solar plants with their locations and technical declarations; a platform administrator links authorized Solcast accounts and approves them. Scheduled jobs publish irradiance and daily energy estimates. Each active location belongs to one active plant and exposes electrical generation at 30-minute or hourly resolution, including capacity limits and optional calibration against production. The internal Region record retains location, provider approval and collection history. The Next.js/TypeScript customer frontend lives in `web/` and runs separately from the Django API and internal admin interface.

Use only provider accounts authorized for this service. Solcast account creation remains an operator task outside this backend. Implement fixtures so development does not depend on credentials or live API calls.

## Stack and portability

- CPython **3.15.2**, Django, Django REST Framework, PostgreSQL and pvlib.
- Dramatiq with RabbitMQ runs background work on native Windows and Ubuntu. Tasks call shared application operations.
- One repository, with API, collection/calibration workers, a dedicated notification worker and scheduler/dispatcher as separate processes. Keep PostgreSQL as the durable source of truth. Queue separation alone does not reserve execution capacity for email; run its worker process independently of provider work.
- Frontend dependencies and their npm lockfile stay inside `web/`: Node LTS, the Next.js App Router, strict TypeScript and types generated from the backend OpenAPI schema.
- API, numerical core and worker must run on Windows x64 and Ubuntu x64. PostgreSQL and RabbitMQ may be local services, remote services or Docker Compose dependencies. Full-stack containers are an additional deployment option.
- Use portable Python entry points, `pathlib`, UTF-8, timezone-aware dates and an explicit `tzdata` dependency. Correctness must not depend on Bash, cron, Unix signals, `fork`, or development reload/watch features.

Python 3.15.2 is fixed. Dependencies and container images are locked to stable
versions; upgrades require installation, migrations, API, numerical and real-queue
checks on both operating systems. The [verification commands](../README.md#verification)
identify the separate optional scientific and live-integration checks.

Build versioned artifacts from the committed dependency locks and promote those
artifacts between environments. Deployment-specific infrastructure configuration and
shared service credentials enter through environment variables; the ignored
working-directory `.env` is a local convenience that never overrides an existing
variable. Plant-specific Solcast account selection, encrypted API keys, resource IDs
and reference settings are administrator-managed database records. EPİAŞ's shared
transparency-service login and Open-Meteo's optional commercial service key remain
deployment credentials; plant EPİAŞ identifiers and weather coordinates remain in
database records. Production secret storage supplies runtime values without placing
them in images or source control, following the deployment/application boundary in
[Twelve-Factor](https://12factor.net/config) and the access and lifecycle controls in
[OWASP Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html).
Run migrations as a separate
release command using the same application version and its dedicated database role.
API, worker and dispatcher processes retain no authoritative workflow state locally:
PostgreSQL owns jobs, leases, results and sessions, and RabbitMQ transports deliveries.
Application logs are an event stream on standard output for the process manager to
collect; `PYRACAST_LOG_LEVEL` controls verbosity and defaults to `INFO`. `django.request`
errors and `django.security` warnings share the console handler; `DisallowedHost` is discarded.
Caught exceptions in calibration, metering and collection scheduling use `pyracast.background.failures.log_failure`: an event key, fields, the
error type and the innermost `pyracast` code location, never the exception message. Local launcher
logs and process identities are development tooling.

A `pyracast.security` line that reports a change (`audit.CHANGE_EVENTS`) and is written inside a
transaction leaves with its commit, so a transaction or savepoint that rolls back writes none and a
process that stops between the commit and its hooks loses it; a refusal, a failed attempt or a
throttle is written at once, also when its transaction rolls back. The one exception is the daily
sign-in lock: its notice is stored through the autocommitted authentication-cache connection, so it
and its `login_locked` line stay when the sign-in rolls back.

Worker and dispatcher entry points handle `SIGINT` and `SIGTERM` without raising
asynchronous exceptions into database operations. The dispatcher finishes its current cycle; the worker stops
taking new work, lets active tasks finish and returns buffered work through Dramatiq.
Its default ten-minute shutdown timeout remains in effect. Deployment must provide
a termination grace period that accommodates active work and broker cleanup.
Abrupt termination still relies on durable leases and idempotent publication;
ambiguous email acceptance is never retried automatically. The local launcher passes
an optional `--stop-file` to its worker and dispatcher; these process-local control
files request the same graceful path on Windows and Ubuntu. Stop and failed-start
cleanup wait for those processes to exit and preserve their identity on timeout.
Legacy managed processes without this option use a best-effort database drain and
bounded termination; queued work can start between the final check and termination.
The production HTTPS gateway and process supervision are provided by the
[Google Cloud deployment](DEPLOYMENT.md): Traefik and systemd-supervised Docker Compose
on one Compute Engine VM with local PostgreSQL, separate schema/runtime roles and
Secret Manager. This initial topology deliberately accepts VM downtime to control
cost. PostgreSQL and RabbitMQ persist on a separate data disk; hourly and
pre-release database dumps go to a private Cloud Storage bucket with seven-day
retention. The recovery-point target is one hour after a successful scheduled
backup; this deployment has no point-in-time recovery. CI promotes immutable tested
image digests and runs migrations separately during a controlled maintenance window.

## Boundaries and records

Use conventional Django apps for accounts, regions, provider connections, forecasts and plants; keep calculations independent of Django. API views, admin actions and tasks call shared application operations. Avoid business logic in serializers or implicit model signals.

| Record | Owns |
| --- | --- |
| Tenant, User, Membership | Customer ownership and permitted actions |
| Region | Tenant, coordinates, IANA timezone and approval state |
| ProviderAccount | Canonical provider identity, encrypted API key, entitlement and account-wide quota |
| SiteBinding | Region-to-provider-account/site mapping and immutable configuration revisions |
| CollectionJob, RequestAttempt | Scheduling, leases, quota reservations and execution history |
| ForecastSnapshot, ForecastResult | Raw input, normalized intervals, saved daily derived outputs and model version |
| Plant, PlantConfiguration | Region ownership and immutable DC/inverter/export capacity and meter-basis revisions |
| PlantSubmission | Typed customer technical declaration before provider configuration is approved |
| PlantIncident, IncidentRevision | Non-overlapping operating periods, remaining capacities and immutable corrections |
| ProductionObservation | Interval energy, quality, source, configuration and recording time |
| ChangeRequest, ProductionChangeItem, IncidentChange | Member proposals, reviewed values and atomic workspace decisions |
| EpiasConnection, EpiasCollectionJob, EpiasSnapshot | Plant identity, independent collection and immutable reported-production acquisitions |
| CalibrationRun, CalibrationActivation | Durable fit, reproducible dataset, held-out evaluation and audited manual/automatic activation history |
| HistoricalReferenceImport | Authorized source settings, immutable export digest and actual acquisition time |
| ReferenceCapacityNormalization | Authorized capacity-only copies, original source lineage and conversion availability |
| ForecastIssuance, PlantEvent | Immutable published electrical forecast and audited plant operations |
| NotificationSettings, ReminderSchedule, NotificationDelivery, OutboundMail | Shared plant email schedule, explicit recipients and language, pinned saved forecasts, grouped delivery outcomes and account and invitation mail intents |
| AuditEvent | Actor, action, target, the details its action declares and timestamp for privileged changes |

Regions select explicit provider accounts rather than embedding credentials. One canonical record per account spans tenants and credential rotations; duplicate records must not create independent quota budgets.

Regions do not store or expose a surface setting: the irradiance result identifies
its horizontal plane. Provider bindings store one `power_basis` for their matched
power and normalization capacity. The numerical boundary still validates
independently declared bases, and archived source manifests retain their fields.

Forecast snapshots pin their job's binding under a composite foreign key, supporting source lookup and a database-enforced normalization target. Normalization retains its unique source/target binding key for idempotency.

## Language

The private application at `/app/` shares URLs across English and Turkish. Language selection uses the account's
explicit preference, then its browser cookie, supported `Accept-Language` ranges,
trusted Cloudflare country information and finally English. Sign-up saves the
language the form was shown in as the new account's preference. Language changes require CSRF; authenticated
choices belong to the current user and survive new sessions and devices.

The customer shell stays neutral until the session supplies its language.
`next-intl` catalogs localize the interface and display formats; Django gettext
covers administration, validation and email. Plant timezones, units, ISO timestamps
and CSV values remain independent of language. CI checks catalog completeness and
interpolation contracts. Gettext binaries are not committed: `scripts/check.py`, the
development launcher and the backend image build compile them; run
`uv run python scripts/translations.py` before starting Django or pytest directly.

Public product, privacy and terms pages render on the server without an API session.
English uses `/`, `/privacy/` and `/terms/`; Turkish uses the corresponding `/tr/`
paths. Their URL fixes the language independently of account cookies, with reciprocal
language links, canonical and hreflang metadata, and a public-only sitemap.
Public sign-in links apply their language to anonymous visitors through the existing
CSRF-protected preference endpoint; signed-in account preferences take priority.
Private pages carry `noindex`; this is not an authorization control. All HTML retains
the nonce-based CSP and private, no-store caching policy. Legal operator information
has one source in `web/src/features/public-site/legal-copy.ts`.

## Approval flow

Company registration creates a non-staff user, a new tenant and its administrator
membership in one transaction. Workspace names need not be unique; registration
always creates a new workspace and never joins an existing company by name or email. The
django-allauth browser API owns registration, email verification, login, recovery,
reauthentication, MFA and session management. Email ownership is mandatory before
login. Django validates passwords with a 15-character minimum and common-password
screening, hashes new passwords with Argon2 and rotates session identifiers on login.
Mutations enforce CSRF before authentication. Allauth's account and IP rate limits
share an atomic PostgreSQL JSON/TTL cache; ingress limits and per-user API throttles on
expensive endpoints additionally protect capacity.
Failed sign-ins are limited per address (also per day) and, under a project action
that no sign-in success or password reset clears, per client IP; a refused account
answers and counts exactly like a wrong password. Only finished failures make the daily
lock: a check that ends in an error releases its slots and one that never ends stops
counting after the 90-second request ceiling. Once a failure completes the lock, and at
every refusal while it holds, the address is sure to have a pause notice from the last
24 hours.

Google and Microsoft sign-in use backend authorization-code exchanges with PKCE
and session-bound state. A same-origin continuation page starts provider navigation
without widening the browser's form submission policy for federated sign-in.
Provider credentials stay in the runtime secret; access
and refresh tokens are not retained. New users complete the same workspace or
invitation signup as password users. Provider identities never grant workspace
access by email or domain, and matching email addresses never merge accounts.
Only Google-authoritative Gmail or verified Workspace addresses satisfy mailbox
verification; other addresses use Pyracast's email verification.
Connections require an authenticated session and recent reauthentication. A
reauthentication uses the strongest factor the account holds: its authenticator, else its
password, else a code sent to its verified email; it ends when the account's credential
state changes. Each session keeps one record of that state, written under the account's
locks when it signs in or confirms; a confirmation checks its factor under those locks.
A write of the account's own credentials (recovery codes, authenticator, password,
addresses, provider identities) runs in one transaction that first
locks the account, answers `session_ended` when the session is gone and asks for a
confirmation by the factor the account needs now when the record differs, also before a
password change that gives the current password. Other requests, such as console writes of
business data, compare the record when they start, so one already past that check finishes,
as a request of a session revoked a moment later does. Linking is
bound to the initiating user, and disconnects lock the user record to preserve a
working login method. Provider sign-in retains Pyracast's local MFA requirements.

Each workspace has one administrator; other members can read data and propose production
corrections or outage changes for that administrator's approval. Workspace
administrators can invite members and change that workspace's customer settings, but
gain no platform privileges. Every API route and method declares its access and throttle
scope in the route policy table of `api/permissions.py`; a route or method without one is
refused. Application operations enforce these permissions again through
`accounts/authorization.py`; mutations check membership, then lock the tenant before its
resources, and reads take no tenant lock. Personal account,
security and language settings remain available to each user. Verified platform
administrators reassign the role at **Workspaces**, choosing an existing active member.
Transfers check the previous administrator, serialize with writes and retain an audit
record. Automatic calibration authorized by a former administrator stops until the
new administrator enables it. Plant reminder settings belong to the workspace and
remain available to the new administrator without depending on the former account.

Workspace administrators can remove another member from that workspace;
they cannot remove themselves or its administrator. Removal preserves the user's
account, other memberships and workspace resources. Under the tenant lock, it records
an audit entry and expires pending invitations for all known addresses of that user.

Invitations grant member access after explicit acceptance by the authenticated
owner of the verified invited email. New invited accounts do not create another
workspace. Invitations expire after seven days and can be cancelled. The notification
worker mints each invitation's token when it sends the mail and stores only its hash. A replacement invitation or administrator transfer invalidates
the previous pending grant. Issuance is rate-limited per workspace and recipient.

Member proposals are immutable and separate from production and incident records.
They do not affect totals, forecasts or calibration before approval. The current
workspace administrator reviews the existing and proposed values, then approves,
or rejects with a reason. Administrators retain direct editing; platform privileges
alone do not authorize workspace decisions. Authors can withdraw pending proposals.

Approval rechecks authorization, the pinned configuration and target revisions, and
the values shown during review. A changed review context requires reloading; an
unchanged provider reacquisition does not. Approval calls the existing production
or incident operation and records its outcome in one transaction, under the
Tenant → Region → Plant → ChangeRequest lock order. Retries cannot apply a request
twice. Conflicting proposals remain pending until explicitly rejected or withdrawn.
Approval and rejection preserve production values at decision time. Detail reads
do not lock collection rows; approval revalidates the displayed context under locks.

The application inbox reads these durable requests: current administrators see
pending reviews and members see their own requests and unread decisions. Reading
a pending request does not resolve it. Administrator transfers change inbox access
immediately. Removing a member retains their proposals for explicit administrator
review, but rejoining does not restore the old membership's author permissions.
Notifications are in the application; this workflow does not send email.

Customers navigate a single plant portfolio. Location and technical details are
collected in three short steps with a review before submission. A plant has one
workspace for forecasts (generation and irradiance), production, calibration,
outages and settings. Approval status remains separate from saved-data availability.
Administrative provider identity and credentials never enter customer forms.
The open portfolio and plant workspace refresh saved records every 30 seconds,
so approval becomes visible without reloading or consuming provider quota.

Administrators use a task-oriented workspace at `/admin/`: Overview for outstanding
reviews, Plants for searchable company and plant records, Operations for collection
and email outcomes, and Settings for the shared recipient limit. Custom Django
views and templates reuse the existing permission, form and application-operation
boundaries. Database model lists are not the primary navigation. Plant approval,
connection health and the last saved forecast acquisition remain separate facts.
Connection setup, validation and approval remain explicit steps; validation consumes
provider quota. Lists read saved records without contacting providers or refreshing
unsaved forms. Email service acceptance is distinguished from an unknown outcome.
The session projection exposes current MFA-verified administration access for
navigation; every administration request independently enforces authorization.
Administrators land in this workspace by default and can explicitly enter the
customer workspace or account security without being redirected back.

Admin health separates current problems requiring action, provider delays and recent
outcomes requiring review, with links filtered to the affected plants or jobs. Recent
outcome counters use the last 24 hours of scheduled time; newer successes do not imply
that earlier failures were resolved. CLI monitoring retains its degraded result for
these recent outcomes even when the admin presents them as review items.

1. A workspace administrator submits a plant, coordinates, IANA timezone and technical declaration together through `sites/`; the location and plant commit atomically with state `pending`. Customer submissions never contain provider credentials. Existing locations without a plant can complete setup through the scoped plant endpoint.
2. A platform admin records the authorized provider account and site binding. The backend validates credentials and site settings within that account's quota.
3. One shared approval operation checks permissions and the validated configuration revision, then atomically records `approved`, materializes the plant's immutable binding-backed configuration, records an audit event and creates the first pending collection job. A pending plant's technical declaration remains editable until approval; the approved declaration remains available afterwards while subsequent technical changes create configuration revisions.
4. A dispatcher delivers that durable job to the worker. The worker retrieves data, calculates and publishes a result.
5. The frontend reads saved results and their freshness. It does not call Solcast.

Keep approval (`pending`, `approved`, `rejected`, `suspended`), connection health and data freshness separate. Approval does not mean a forecast is ready. Coordinate or model-setting changes create a new revision requiring validation and approval; saving unchanged settings or only a new API key keeps the revision and approval; stale jobs must not publish into the new revision.

PostgreSQL permits one plant per region and one active location per tenant, comparing latitude and longitude rounded to six decimal places. The application uses the same rounding for new coordinates. The same location in another tenant is independent. Trashed locations and their immutable archives remain intact; restoring a location rejects an active replacement conflict. Provider reapproval creates a new configuration revision from the current technical parameters when the binding changes, preserving prior configuration and source identities.

Workspace administrators can rename a plant and its region without changing a revision or approval. Location changes are reviewed requests pinned to the current revision and moved along when a binding replacement bumps it; one pending request per region is allowed, and a target that another active location holds or another pending request names is refused at submission. The requesting administrator may withdraw a pending request; the platform administrator's decision may carry a note the workspace sees. A region never approved and without a binding for its revision moves at once, audited as a request its administrator approved. Platform administrator acceptance applies a new pending revision and removes the old approval. A replacement binding must be configured, validated and approved before collection resumes. Rejection preserves the current location.

## Reliability and security

- Keep one authentication authority: django-allauth browser sessions, with no app-token endpoints or separate password login. A platform administrator is an active staff superuser; the console also requires MFA evidence bound to the current user and a live authenticator, and every console write a reauthentication by that authenticator within the reauthentication window, which the API's MFA management uses too. Administrators require a TOTP factor provisioned through `bootstrap_admin` before login; password possession cannot bootstrap an administrative factor. Reauthentication protects security changes, and recovery codes are one-use and shown once. MFA secrets use authenticated encryption with an HKDF context distinct from provider credentials.
- Derive tenant access from authenticated membership. Only console views pass platform authority (`platform=True`) to a shared operation, which checks the platform administrator before it takes the tenant lock; `/api/tenants/` routes never do. Scope list/detail/create/update operations, worker jobs and cache keys. Tenant ownership is not a trusted request field. Django admin does not provide tenant isolation automatically.
- Store provider keys in a secret manager or authenticated encryption with keys outside the database. Accept credential changes through write-only admin inputs; redact secrets from responses, logs and audit payloads.
  The MFA-protected administration setup page supports existing authorized accounts
  or a new private account, followed by explicit connection validation and approval.
  Pasted keys use Fernet authenticated encryption, with an HKDF-SHA256 key derived
  from the externally configured Django secret and a versioned application context.
  Solcast inputs are write-only raw API keys; persistence accepts encrypted credentials
  and never resolves environment references. Existing-account key changes require
  explicit confirmation and invalidate validation for every binding on that account
  without replacing the canonical identity or resetting quota. Key rotation
  can decrypt with configured secret-key fallbacks; `reencrypt_secrets` rewrites stored
  credentials under the new primary key before the old fallback is removed. Back up
  the external keys separately from the database. This protects database-only
  disclosure, not compromise of the application host that holds the decryption key.
- Reserve quota atomically by provider account and UTC day before each request, including validation and retries. Uncertain request outcomes consume budget conservatively; do not rotate accounts to evade limits. Solcast HTTP 429 records a shared account cooldown; HTTP 402 (daily allowance used) blocks the account until two minutes after the next UTC midnight; HTTP 401 blocks new requests for the failing credential revision until key rotation. These gates stop request admission without disabling access to previously authorized saved forecasts. HTTP 403 is a permanent failure of that request, not an account-wide credential verdict.
  Credential rotation preserves account cooldowns. A 429 without a usable retry
  hint waits at least 15 minutes. An audited administrative reset requires the
  operator to confirm provider recovery and changes neither request usage nor
  authentication state. Successful validation can re-arm the latest approved
  forecast job that failed authentication, without clearing its request ledger.
- PostgreSQL owns retry scheduling, attempt budgets and leases. Dramatiq actors retain `max_retries=0`; HTTP adapters make one ledger-accounted request per attempt. Retry transport failures, HTTP 408/425/429 and transient 5xx responses; HTTP 402 waits for the provider day; other HTTP failures, including 501/505, require a later configuration or service change. Each stored outcome names the answer. An undecryptable key or malformed site id fails before a request is reserved. Provider and weather reads stop at 2 MB or 30 seconds. Preserve safe error codes and `Retry-After`, accepting delta seconds or an HTTP date as a minimum delay without shortening long hints. Add 0–30 seconds of jitter to bounded exponential backoff. Forecast jobs allow three execution attempts, including interrupted lease recovery; admission waits consume neither a request nor an execution attempt. Transient forecast-provider failures wait at least four and then eight minutes, so a five-minute outage can recover within the 15-minute preparation grace without increasing the three-request budget; calculation failures retain separate backoff. A newer configured forecast slot supersedes older unclaimed work, preventing an outage from replaying an obsolete backlog. Account quota and forecast reservations still apply to every actual retry.
- Claim jobs with expiring leases and unique scheduling keys. Assume queue redelivery: persist results idempotently and recover interrupted work. Publish through a durable job/outbox record so a database commit cannot lose the collection request. Keep HTTP connection/read/write/pool limits distinct from job and business deadlines: HTTPX timeouts bound individual I/O waits, and Dramatiq's best-effort time limit is not a hard network deadline.
- Workspace administrators configure region-local daily forecast times, initially 16:00. Historical collection runs separately at 00:00 local time, refreshing yesterday and recovering missed days from the available seven-day window. Request `hours=168` explicitly and archive completed intervals; each acquisition remains immutable. Historical transport failures, empty responses and incomplete previous days retry no sooner than 30 minutes later, before 09:00. Restart catch-up is allowed inside that night window; disabling collection stops new night work. Account-wide UTC quota reservations preserve the remaining scheduled forecast needs before admitting a night request. Quota exhaustion or reserved forecast budget never creates a rapid retry loop. Configuration changes apply to future forecast slots; they do not invent missed forecast acquisitions. Reuse the pinned slot of existing work for the same target day and night deadline when the night start changes. Use unique slots, advance a nonexistent night start to the first valid half-hour before 09:00, choose the first repeated time and flag late acquisitions.
- Do not hold database transactions open during external HTTP calls. Recheck binding revision and approval before publication. Multiple scheduler instances must not duplicate work.
- Preserve forecast vintages and configuration/model versions. Expose stale or incomplete results explicitly. Monitor last successful collection, queue lag, provider errors, quota, solver failures and coverage; logs carry correlation IDs without credentials.

Provider latency and dispatcher polling can extend retry times beyond the preparation
grace; the reminder then uses its saved fallback at the fixed cutoff while collection
continues independently.

The dispatcher isolates scheduling and publishing phases so a provider or broker
failure in one queue does not prevent the remaining phases from running. Failed
publishing leaves the database job ready for a later cycle; duplicate broker
deliveries still pass through its lease. Deployment supervision must restart failed
processes and monitor dispatcher progress and overdue work independently of the
email channel it protects.

Application database connections use a five-second connection timeout, a
five-second PostgreSQL lock timeout and a 60-second statement timeout, including
the authentication cache connection. The release migration command temporarily
uses its explicit migration limits and restores the application limits afterward.
Test database creation and removal use a separate five-minute statement timeout;
application and authentication-cache sessions keep their normal limits during tests.
SMTP sockets and Gmail API I/O use 15-second timeouts. These are connection and individual
operation limits; business deadlines are checked separately after database waits.

`DispatcherStatus` records each phase's last attempt, success, safe error code, duration and published count.
`check_delivery_health --max-lag-minutes 5` emits JSON and fails for missing, stale
or failed phases, overdue ready jobs, expired active leases, recent
failed/uncertain/unavailable or incomplete tomorrow deliveries, missed delivery
deadlines, EPİAŞ failures, credential blocks, account cooldowns, mail
authentication failures and overdue, failed or uncertain account and invitation mail. Enabled approved regions without a successful forecast
in 26 hours are unhealthy. Complete saved-forecast fallback remains informational.
This check needs a working database; monitoring
must also treat failure to execute it as unhealthy. Run the mailer with
`worker --queues notifications --threads 2`, separately from the default collection,
calibration and EPİAŞ queues. Restart all workers during rollout: an older worker
continues consuming all queues until stopped.

## Database invariants and rollout

Use Django migrations as the sole schema owner. The migration dependency graph,
historical models and `django_migrations` ledger travel with the application.
The history starts from one `0001_initial` per app (`connections` adds
`0002_initial` to break its cycle with `regions`), built for a reset database.
Applied operations and data transformations are immutable; fixes use new forward
migrations. The overridden `migrate` command refuses, before it changes anything,
a database whose ledger names a project migration that is not on disk: such a
database was built from replaced migration files and must be reset or restored
from a backup taken after the reset. Historical model type annotations can be
corrected without changing replay behavior. Do not add Flyway or Liquibase over the
same tables. `makemigrations --check --dry-run` checks model state against committed
migrations, while `migrate --check` checks unapplied migrations; neither is a
physical-schema drift detector or a migration checksum. Do not make unrecorded manual
DDL changes.

PostgreSQL 18.6 is the verified, pinned development/CI version. Keep time in
`timestamptz`, quantities in explicitly named units, unknown measurements nullable,
and immutable acquisition payloads separate from typed relational identities.
Retain bigint identity keys: replacing them with UUIDv7 has no demonstrated need.
JSONB holds provider payloads and versioned numerical artifacts; foreign keys,
scheduling selectors and quantities needing database rules remain typed columns.

Partial unique constraints enforce one active plant/location. GiST exclusion
constraints enforce half-open, non-overlapping observation and operating periods,
including the cancelled-incident predicate. PostgreSQL 18 temporal syntax does
not replace that predicate. Deferred composite foreign keys additionally enforce
same-region approval, request/account/binding provenance, configuration ownership,
calibration ownership and acquisition/job identity. Explicit region, plant and
binding ownership keys on configurations, production jobs and forecast snapshots
let PostgreSQL enforce both paths of each relationship. Normalized copies must
point to a different source snapshot and to an acquisition for their target binding;
the source/target uniqueness key still prevents duplicate normalization.
CHECK constraints reject invalid ranges, non-finite physical values and unsupported
persisted states.
Composite foreign keys, the issuance trigger and the `btree_gist` extension, which
models cannot declare, are listed once in `pyracast/schema_objects.py`; the migration
that creates each one and the schema catalog test both read that list.
Constraint and index names follow `<model>_<subject>_<kind>`: the lowercased model
class, the constrained columns (or one word for the rule) and one of `chk`, `uniq`,
`excl`, `fk`, `idx`; index names stay within Django's 30 characters. A rename changes
only the name: PostgreSQL rebuilds and revalidates nothing. A region in the trash hides
its plant and every row under them; readers ask the region and plant models what is
active and never filter `deleted_at` themselves.
EPİAŞ archives use protected relations; only the explicit expired-definition purge
removes their snapshots, jobs and connections in order.

These rules supplement tenant-scoped, locked application operations. They are not
row-level security. Publication eligibility, access and source-selection policies
still belong to shared operations and workers. Direct SQL must not bypass those
operations to create business records.

Provider account identity uses PostgreSQL 18 `casefold()` with the built-in
`pg_unicode_fast` collation and an explicit Unicode whitespace trim set. The same
expression governs the unique index, lookup and insertion, independent of the
cluster's operating-system locale. PostgreSQL's Unicode version is authoritative;
Python's Unicode version must not introduce a second identity definition. No
Unicode normalization or accent folding is applied.

The overridden `migrate` command retains Django behavior and uses a database-scoped
session advisory lock on the same direct connection. Concurrent migration runners
fail fast; session-level DDL lock and statement timeouts are restored afterwards.
The command rejects Django connection pooling and refuses further queries if its
locked physical connection is replaced, rather than continuing without the lock.
Use one deployment migration job with a dedicated schema-owner role. Production
API/worker roles should have only required DML/sequence access, no superuser,
CREATEDB, CREATEROLE or schema CREATE rights. Provision `btree_gist` administratively
when the migrator cannot create extensions. Local loopback trust/superuser helpers
are test conveniences, not production provisioning. Production connections require
authenticated transport and secrets outside the repository; use a direct endpoint
for migrations even when application connections use pooling.

Review `migrate --plan` and `sqlmigrate` before deployment. Test both an empty
database and a restored copy of existing data. For a small database with stopped
writers, ordinary constraint/index creation is appropriate. At larger scale,
create each concurrent index in its own non-atomic migration; add nullable
columns, backfill in bounded batches, switch readers/writers, validate, and only
then remove obsolete fields. Never wrap the entire migration sequence in one
transaction. Keep FK alterations and their constraint replacement atomic. A failed
concurrent index build can leave an invalid index: inspect `pg_index`, remove only
the failed migration's index with `DROP INDEX CONCURRENTLY`, then retry the same
migration. Do not mark the operation applied with `--fake`. A failed validation is
a data investigation, not permission to delete
history. PostgreSQL 18 skip scan and AIO are planner/runtime capabilities; use
measured query plans and platform-supported defaults rather than speculative
partitioning, duplicate indexes or Linux-only `io_uring` settings on Windows.

Take a custom-format logical backup and prove it restores before a local schema
change. Production recovery additionally needs retention, off-host backups and a
tested recovery objective. This deployment uses hourly logical backups; workloads
requiring a shorter recovery point need monitored WAL archiving/PITR. Back up external
encryption keys separately. After restoring to an earlier point in time, suspend
provider requests for the rest of the UTC quota day unless its complete request
ledger can be recovered; otherwise the restored ledger understates consumption.
Prefer a forward repair; a reverse migration may discard data and is not an
automatic rollback strategy. Run `ANALYZE` after a
restore, and verify all constraints are validated and all indexes valid after
deployment. Major PostgreSQL upgrades require their own tested `pg_upgrade` or
dump/restore procedure; minor upgrades follow the release-specific instructions.

References: [Django migrations](https://docs.djangoproject.com/en/6.1/topics/migrations/),
[staged constraints and concurrent indexes](https://docs.djangoproject.com/en/6.1/ref/contrib/postgres/operations/),
[PostgreSQL 18 features](https://www.postgresql.org/docs/18/release-18.html),
[backup and recovery](https://www.postgresql.org/docs/18/backup.html).

## API and delivery

EPİAŞ production uses a separate `metering` app and durable `epias` queue. Only an
MFA-verified platform administrator connects, changes or disconnects a plant by
provider ID and EIC from its administration page. Tenant members can view the
connection status and saved production archive; server-side `EPIAS_USERNAME` and
`EPIAS_PASSWORD` obtain a cached TGT without exposing credentials to the browser.
Collect 90 completed Istanbul dates of reported production on connection or a new plant configuration,
then reconcile the trailing seven dates at the region's independent 00:00 nightly
slot. The previous day's reported-production job retries incomplete or failed
responses every 30 minutes before 09:00, respecting longer provider retry hints.
Older correction dates and settlement jobs retain three attempts. When initial collection
starts inside an enabled nightly window, its previous-day job uses the remaining window
and the same morning deadline. Other initial backfill remains independent of the nightly
schedule. All jobs with a morning deadline stop when collection is disabled.
Resolve the separate settlement plant ID by EIC and initially collect
the previous twelve completed calendar months, including when the settlement identity
becomes available after connection. Use one paginated request period per month;
reported-production backfill stays at 90 days. Reconcile the previous two months daily
and the third on Mondays. Monthly paginated requests produce daily immutable
snapshots. Unpublished or partial settlement periods wait for the next scheduled
reconciliation; they are not zero production. Jobs are independent of Solcast
requests and its account quota. Expiring leases protect execution and redelivery.
Initial jobs use a stable connection/configuration timestamp, so repeated dispatch
does not duplicate the backfill. Existing connections receive the missing initial
window. This collects available production history; Solcast rooftop estimated
actuals still have a seven-day retrieval window and accumulate through daily
acquisitions. Ninety production days do not imply 90 matched calibration days.
Pin plant configuration, provider identity and connection revision; recheck active
ownership/configuration before publishing. Disconnect stops future collection and
retains the archive. Definition deletion uses the existing recycle-bin lifecycle.

The plant's **Production** workspace combines manual readings, reported production,
settlement injection and CSV preview/import. Effective selection prefers manual
readings, then valid UEVM values hour by hour, falling back to the latest reported-production acquisition
with its provisional label. Preserve
native hourly labels, MWh, missing values, collection/publication times and a
payload digest. Policy `hour-start-istanbul-v1` treats 10:00 as [10:00, 11:00).
Missing provider rows have null values and an explicit `reported: false` flag.
Latest revisions never fill their missing hours from older acquisitions.
The reported total sums valid selected hours once every hour intersecting astronomical
daylight is available. It includes reported nighttime values; missing nights remain
null and do not block the total. `complete` still requires all 24 provider hours.
Use the archived site's coordinates; polar nights require all 24 hours.

Net-export calibration selects manual observations, then valid UEVM hours, then
provisional reported-production hours. The latter are explicitly labeled proxies;
they do not establish the meter boundary. Forecast-performance evaluation remains
limited to manual observations and UEVM. Manual measurements of any quality override an
overlapping whole provider hour; never split that hour into invented half-hours or
replace an outage label with provider data. Keep source snapshots separate from
manual observations. Pin measurement and reference snapshot IDs when requesting
calibration, and reject publication or activation after the plant's source identity
changes. Pin the measurement-selection policy as well as both production source
kinds. Existing runs retain their settlement-only policy. Content fingerprints
prevent unchanged acquisitions from repeatedly requesting the same fit; an RT-to-UEVM
source change prepares a new candidate even when the energy values match. Publication
of either production or Solcast estimated actuals checks for newly matched data.
Source suitability does not bypass matched weather,
daylight coverage or statistical acceptance requirements.

Read-only interval tables show the first through last positive value, retaining
interior zeros and gaps. All-zero days remain distinguishable from missing data.
Forecast tables also offer the full local day, exposing missing edge intervals.
Coverage notices distinguish missing source hours from values whose irradiance
cannot be inferred; neither becomes zero or a complete daily total.
The entry journal instead preserves available reference slots and saved/draft
measurements. Numerical views offer UTF-8 CSV downloads containing the entire
selected dataset, including hidden edge intervals, units and provenance. CSV cells
preserve missingness, precision and quoting, and neutralize spreadsheet formulas
in text fields.

Route only what the customer interface calls: plant submission and listing, dated forecast retrieval and the workspace operations; provider setup, validation and approval live in the operator console. The customer DRF endpoints accept JSON bodies only. Generate OpenAPI; use explicit units, bounded date ranges and pagination. Every `/api/` failure answers one localized body, `{code, message, fields?, retry_after?, request_id?}`, whose codes are a closed vocabulary exported to OpenAPI. The adopted allauth sign-in views keep allauth's own keys and carry the same contract keys beside them, the code taken from the HTTP status and the message from allauth's first error. A CSRF failure redirects back to the app only on the provider sign-in redirect and callback routes, which a browser reaches by form navigation. User text says "outage"; `incident` stays the internal name. Approval transitions are operations, not writable status fields.

Workspace administrators can move a region, with its one plant, to a tenant-scoped recycle bin; a plant is never deleted on its own. Keep data for 30 elapsed days from the first deletion; repeated deletion never extends the deadline. Normal reads and writes hide deleted regions and their children, and deletion stops collection, calibration and reminders. Restoring a region preserves approval and configuration and resumes future schedules without catch-up. Already submitted email cannot be recalled.

The dispatcher permanently purges expired definitions and their owned records in bounded transactional batches. Restore is unavailable at the deadline even if a purge batch is pending; the bin and the restore action take their blockers from `trash.restore_blocker`. A completed purge logs one `trash_purged` record with the region, tenant, deletion time, plant ids and each binding's provider, account and site id, and no name, coordinate or key, so an operator can remove orphaned provider sites. Retain shared provider accounts and account request-attempt counters without deleted region/job links, so deletion cannot reset quota. Preserve historical data during the recovery period; deleting a definition is separate from replacing a forecast vintage.

Serve the frontend, `/api/`, `/admin/` and Django's `/static/` assets through one
origin. The Next.js development server proxies Django routes and assets; the
production gateway owns this routing. Django owns authentication and
authorization; the allauth browser API enforces CSRF even before login. Sessions
use HttpOnly, SameSite=Lax cookies, with Secure and a `__Host-` prefix in production.
The session cookie lasts until the browser closes. A response sets it only when its
request created or rotated the session key and deletes it only when its request ended
the stored session the cookie named other than by expiry (a sign-out, a password
change); an expired session leaves its key, which names no session. So an answer
arriving after a newer one never restores an older key, and an expiry never deletes the
key of a later sign-in; a request whose session no longer exists answers as signed out.
The web client treats that answer as the end of the session only after a session read
sent after it agrees, and otherwise repeats the refused request once when that read shows
the account the request was prepared under; a repeat refused
again goes through the same read and is not sent a third time. A sign-out whose session
read still shows the same account signed in is sent once more with the current key, and
reported if the session is still signed in after it or the read shows another account.
Revoke sessions after 12 hours or 30 minutes without requests; polling of a visible
tab counts as activity, and hidden tabs do not poll. Sessions predating the lifetime markers fail closed. A request
whose session another session of the account ended while it ran answers 400
`session_ended`, also when its own work committed; the client signs in again. A
provider callback only verifies: it stores the verified login for its stored session while
the provider state lives and sends no session cookie (a session that ended before it took
its state finds nothing pending). `POST auth/provider/complete` signs in or connects from
that login once, a connection inside the account's credential write. API
responses are private and non-cacheable. The frontend reads stored results and
never handles provider credentials. A fixed public HTTPS origin determines
security-email navigation. Account and invitation mail is stored as an outbound mail
intent in the transaction of its request (or of `reset_mfa`, for its notice) and sent by
the notification worker, so no answer
waits for or reveals delivery; a code is kept only sealed with the application key and
erased when the intent ends, and unknown-account and account-exists notices share 20 a
hour. Every mail is transmitted at most once: an intent interrupted before its transmit
marker is retried, and one interrupted after it ends `uncertain`, holds no code, is
reported in health and the console and is never resent. After a failed or uncertain
mail the person asks for a new reset code at once and for a new verification code once
its one-minute resend window has passed, which no mail outcome shortens, so that answer
is alike whether the address has an account (which gets a notice instead of the code);
the administrator sends a failed or uncertain invitation again at once, while an
uncertain notice (the daily sign-in pause included) or reminder slot stays sent
at most once and the next slot sends normally. Verification and recovery codes expire
after ten minutes, allow at most three attempts and are bound to the initiating browser
session. A pending code or sign-in step, and a reauthentication kept in the session (a
console confirmation included), rests on the credential state of its account (password,
active flag, addresses, provider identities, authenticators) as read under lock where
the request identified the account or recorded the reauthentication; any change of that
state ends every pending step and reauthentication of the account, the operator commands'
changes included, a state equal again counts as the same, ending sessions by hand ends
none, and a step without an account (a provider sign-up, the new address of a change)
is guarded by the unique constraints when it completes. Starting or completing a step,
recording a reauthentication, an operator command on an account and reclaiming an
abandoned sign-up lock the account's addresses, provider identities and account row, in
that order, before its authenticators or workspaces. Real
mail delivery through the Gmail API or SMTP is a production requirement, separate
from mocked mail validation. Gmail uses only `gmail.send` with OAuth credentials
for the configured sender; credentials remain deployment secrets.
Local development may explicitly select Django's console backend with
`PYRACAST_EMAIL_BACKEND=console` and `PYRACAST_DEBUG=1`; messages, including usable
verification codes, go to private local process logs. Console mail in production or
an unknown backend stops startup. Email replacements require reauthentication
and verification; a password change also needs a recent MFA reauthentication when the
account has an authenticator, and invalidates other sessions. Customers can optionally
enable TOTP from account settings. Enrollment renders the server-issued setup URI as a
local QR code with a manual-key fallback. The browser keeps secrets only in component memory.
Removing TOTP or accessing and regenerating recovery codes requires explicit MFA
reauthentication within five minutes, bound to a still-enrolled factor; password
reauthentication is insufficient. Platform administrators cannot remove TOTP.

Requests sharing a valid session cookie serialize through a PostgreSQL advisory
lock around session loading and response saving. This prevents concurrent code
attempts or ordinary polling from overwriting security state. Acquisition waits
at most five seconds before returning HTTP 429 with `Retry-After`. The provider
callback ends its serialization once it has taken its state, so provider calls hold no
lock and no transaction. The default database connection must remain
on one PostgreSQL session throughout the request: use a direct connection or an
external session pool, never transaction-pooled PgBouncer. Django's built-in
connection pool is rejected. Connection loss fails closed and releases the lock.

Forwarded headers are accepted only from explicitly configured immediate proxy
CIDRs. The gateway sanitizes untrusted forwarding headers and appends its actual
peer; production additionally trusts the reviewed Cloudflare edge ranges. For
this one-edge topology, Django uses the gateway's peer or the client immediately
before that edge, ignoring any earlier visitor-supplied prefix. A client inside a
trusted edge range remains the client; the parser never skips additional hops.
Direct untrusted peers cannot supply either identity. Authentication rate limits use
strongly consistent PostgreSQL cache reads and conditional upserts for atomic
addition, retain live entries without eviction, clean expired entries in bounded
batches and propagate database failures. The cache uses a dedicated autocommit
connection to the same database so application rollbacks cannot reset limits or
used OTP codes; the database clock owns TTL expiry. IPv4-mapped addresses share the
IPv4 identity and allauth groups IPv6 limits by /64 while audit logs retain the full
validated address. No process-local cache or alternate login
may bypass these limits. Keep ingress rate limits and scheduled session cleanup in
deployment. Size PostgreSQL connection capacity for both persistent application
and authentication-cache connections per serving thread.

Keep the customer interface compact and responsive: primary values and actions stay visible; source details and extended guidance use disclosures. Preserve units, missing values, quality warnings and CSV access. Tables scroll within their own focusable region; navigation and forms fit narrow screens. Show the journal's local timezone once, retaining interval offsets when a daylight-saving transition makes local times ambiguous.

Weather supplier identity and account/site details remain internal. Customer JSON, email and CSV use neutral source labels while preserving units, missingness, source kinds and quality warnings. Delivery projections never rewrite saved provenance; operator provenance reaches customers only through typed allowlists, and account-level provider usage stays in the console.

Auxiliary Open-Meteo weather is the narrow attribution exception: its license requires visible source and license links next to displayed data. Worker forecast deliveries archive its native hourly GHI, temperature endpoints and ground-snow indicators separately on the Hobby forecast snapshot, without using additional Solcast endpoints or quota. Regional results expose an independent weather reference with its own energy, coverage, collection time and attribution; weather failure does not prevent the Hobby result from publishing. Neither source fills the other's gaps. Existing emails and CSV exports retain their original dataset. The optional customer API key lives only in environment configuration; the free endpoint is for evaluation/non-commercial use.

Plant operations use the same tenant membership boundary. Configuration revisions pin the approved reference binding and distinguish DC nameplate, inverter AC capacity, grid export limit and the measurement point (`net_export` or `ac_generation`). Unknown capacities remain null. Changing the configuration (any parameter or the binding) creates a revision and supersedes pending calibration work, while an unchanged or name-only save keeps it; it does not rewrite historical forecasts or observations.
Mounting metadata is also revisioned: unknown, fixed or tracking, with optional fixed-array tilt and clockwise-from-north azimuth. A tracking declaration over a horizontal reference with declared DC capacity selects the tracking geometry transfer in `IRRADIANCE.md`; fixed-array angles are not consumed. In particular, a 100 MW grid export limit does not imply a 100 MW inverter rating.

The plant's **Outages** tab records actual operating periods, their cause and
optional remaining DC/inverter/export capacity in MW. Zero means unavailable;
unknown values remain null. One combined period represents simultaneous causes,
with PostgreSQL enforcing non-overlap. Edits correct the whole reported period;
a change in operating capacity requires closing the old period and starting a new
one. Tenant-scoped writes lock the plant and require the expected revision.
Resolution and cancellation retain immutable evidence. Capacity reports must be
reconfirmed after a plant configuration change before numerical use.

Live outlooks show normal and conditional affected scenarios while an outage is
active. Resolution restores the single current/future outlook; completed past
days retain the historical scenario. Explicit `as_of` uses only incident revisions
known then. Archived forecasts are not rewritten. Overlapping meter intervals
are excluded from calibration without modifying saved readings. Fits pin incident
revisions; relevant later corrections supersede pending work and prevent a stale
factor from being activated or used in new outlooks. Preparation can create a new
candidate when enough unaffected matched days remain.

Current/future forecast outlooks expose `production_diagnostic` from the latest
applicable completed analysis, including insufficient and rejected fits. It compares
matched production with the pinned eligible active factor or explicitly uncalibrated
factor 1, and reports daily evidence independently of its multi-day median. Complete
valid zero-production days remain visible; evidence older than two completed local
days is labeled stale. It changes neither forecast power nor inferred capacity.
Only reported operating limits drive outage scenarios. A suspected multi-day
shortfall pauses automatic promotion of fitted Huber candidates over an active
calibration. Explicit identity recovery may pass that diagnostic veto after all
other promotion guards pass; this does not identify the cause of changed production.
Details are in
`IRRADIANCE.md`.

The Production workspace selects a completed local day and preserves native measurement intervals and explicit gaps. Astral calculates daylight from immutable binding coordinates and timezone without provider requests. Whole intervals overlapping daylight remain visible. The customer timeline combines manual readings and saved provider values; previous-configuration observations remain display-only history beside every interval they overlap. Explicit grid API access still supports 30/60-minute views, summing two contiguous manual half-hours without splitting provider energy.

Workspace administrators can enter or correct completed native 30/60-minute intervals under the current approved configuration, independently of reference-weather availability. Calibration still requires matching archived estimated actuals. Input uses explicit MWh or kWh, timezone-aware bounds and quality labels. Batches contain at most 2,000 non-overlapping rows within 31 local days. Each change checks its expected head revision; a change whose token was already superseded by the identical value is a replay and answers as the first call did, while other stale input rejects the entire atomic batch. A value whose mean power exceeds 1.25 times the plant's power ceiling is saved only with explicit confirmation. CSV preview takes no row lock, captures those same tokens and warns about such values and when partial measurements suppress a whole provider hour without covering its remainder. The insert-only `import_production` command retains reference-coverage validation and idempotent exact replays.

Observation values remain immutable: corrections append a revision and explicit revert appends a withdrawal, returning selection to the latest eligible provider value or missing data. Current-head metadata supports PostgreSQL uniqueness and exclusion constraints using `btree_gist`; historical selection uses the recorded-time cutoff and observation ID watermark, including replacements with identical timestamps. Writes and calibration watermark capture serialize under the plant lock. Corrections are absolute energy values: newer provider acquisitions refresh the displayed original and provenance without changing the correction. Preserve native bounds, actor, recording time and revision history.

Saving production automatically prepares a durable analysis job once at least one day has complete, valid matched daylight coverage within a 60-day pool ending on the latest recorded local day. One to six days produce diagnostics with an insufficient fit; fitting requires seven matched days and five informative training days. Complete valid zero-production days remain in the chronological holdout and its scored errors, but are excluded from factor learning. The fitter trains on the 30 calendar days before the final untouched holdout. Additional readings can be saved while a job is running; terminal completion prepares one follow-up if new eligible data arrived. An unchanged dataset does not trigger repeated jobs; a run that failed without a data error is retried after one day. The manual API retains its 7–90 completed-day range for operator compatibility. A separate Dramatiq `calibration` queue performs offline analysis without provider calls; leases expire after five minutes and permit at most three attempts. Pin observation creation time and ID watermark, source snapshots, configuration, selection policy and a dataset digest. Recheck configuration and lease ownership before publication. Authorized members can enable daily automatic checks and guarded promotion before any fit is ready. Promotion requires at least thirty matched days, current Hobby source validation, the candidate-specific improvement and bias guards, and seven new holdout days after the previous promotion. Disabling keeps the last good selection; baseline reset disables automation, and configuration changes clear its authorization. Activations are audited separately from immutable fits, with a null actor for system decisions and the authorizing member in event details. Details and statistical acceptance criteria live in `IRRADIANCE.md`.

Platform administrators can preview and import historical rooftop PV JSON/CSV exports from a plant's setup page. Compare settings present in the file with the approved reference; reject conflicts and record missing settings as an explicit operator declaration, not file verification. Normalize offset-aware timestamps to UTC; naive timestamps require an explicit source timezone and ambiguous local times are rejected. Preserve native 30/60-minute intervals. The signed, expiring preview binds the file digest, parser options, administrator session and reference configuration; saving rechecks authorization and configuration under database locks. The command-line importer retains its full settings manifest and trusted-path requirement. Store the original rows and digest, provenance and real import time; repeated imports are idempotent and corrections create new acquisitions. Commit the archive before one calibration preparation, without provider requests. Imports remain distinct from native acquisitions and cannot independently authorize automatic promotion; an undeclared reference DC capacity prevents settings-matched pooling.

The current/future plant outlook uses `latest-forecast-coverage-v2` for the current approved configuration. Each published forecast owns its coverage window in newest-first order; older forecasts fill only time outside newer coverage, so an intraday update does not discard the morning. Nulls, missing rows and zeros inside newer coverage remain authoritative. Read coverage metadata before loading native payloads: only snapshots owning part of the requested window require native-row validation; invalid contributing sources still fail the request. Record source snapshots, coverage segments, native interval bounds and per-output interval source IDs. Both normal and outage scenarios use the same composition; freshness follows the oldest contributing acquisition. Collection at each configured time refreshes this outlook, including intraday updates; open forecast screens refresh saved results every 30 seconds without provider requests. Historical electrical forecasts and explicit `as_of` queries select immutable electrical issuances under the pre-daylight policy below. Calibration instead pairs production with complete native Hobby estimated actuals when available, otherwise complete historical acquisitions, applying the request cutoff before source ranking; it never falls back to forecasts. Preserve immutable acquisitions, source kinds and publication times. Later revisions cannot change a completed fit or an archived forecast. Missing history yields insufficient calibration data.
Calibration jobs pin the incumbent activation under the plant lock. An eligible candidate must satisfy the current numerical policy and cannot replace a different activation selected after fitting was requested. Policy `regularized-huber-v7` retains the Huber challenger and permits explicit `identity_recovery` to factor 1 only after paired holdout MAE and bias guards pass against a nonidentity incumbent; it preserves source, support, freshness and authorization gates. A complete zero-production holdout day with positive matched reference rejects either candidate, while its dates and prediction errors remain scored. Archived performance uses the same frozen issuance as the archive screen, including its original calibrated and factor-1 native power vectors. Measurements belong to that issuance's configuration; different measurement bases or timezones are not combined. It scores valid zero-production days even during reported incidents and exposes requested/matched/missing/excluded days and scored interval/hour coverage. The frontend uses daylight scope; the API also retains explicit full-day evaluation. The statistical contract is in `IRRADIANCE.md`.

Production comparison opens the separate `historical_model` mode: saved estimated actuals and reported production under the current configuration with the calibration fitted for each period, next to a factor-1 baseline. Retrospective configuration assumptions and provisional production remain labeled. Archived forecast evaluation stays separately selectable. Show matched days in the table, summarize unavailable dates in a disclosure and use a compact empty state when no pairs exist.

The plant screen opens tomorrow in the region's timezone at 60-minute resolution. Its ordinary automatic source selects forecasts for today and future days. For completed past days, it switches to Solcast estimated actuals when finite, nonnegative source power covers the full astronomical daylight window; missing nighttime values do not block this switch. Polar night instead requires complete native coverage of the local day. Until then, retain the saved forecast composition published before that day's end, with the configuration and calibration available then, including the compatible reference-transition fallback below. Label it `source_kind: forecast`, `forecast_fallback` and `fallback_reason: estimated_actuals_unavailable`, preserving its selection cutoff and source snapshots. Later acquisitions cannot enter this fallback. Regional automatic outlooks use the same daylight readiness and saved-forecast cutoff, composing only one archived binding. Without a usable saved forecast, preserve the available partial actuals or an unavailable estimated-actual result. Explicit estimated actuals continue to expose partial history, and archived electrical issuances retain their separate pre-daylight policy. Ordinary past-day actuals prefer the current configuration when its binding archived the day, flagged `historical_configuration_assumed` for days before the declaration. They apply the newest activated calibration whose training period had begun by that day (`retrospective_calibration`); earlier days use factor 1, and a baseline reset ends that history. Otherwise they use the last configuration known at local day start, or the last declaration during that day if none existed at its start. If no configuration existed that day, use the first known declaration as an explicitly assumed retrospective estimate. Later binding revisions do not hide the selected day's archived source. Explicit `as_of` and historical-model performance comparisons retain their separate selection rules. Explicit source selection preserves access to both series; the API defaults to forecasts for compatibility. A missing configuration or source returns `freshness: missing`, a null result and `unavailable_reason`. Explicit `as_of` estimates and days served by an earlier configuration use factor 1. All historical electrical estimates label their retrospective assumptions; they are not metered output. Configuration revisions are never backdated to fill a gap.

For today's ordinary automatic view, an intraday reference-capacity change can leave the current binding with only evening coverage. If the physical plant parameters and the reference account/site, location, timezone, geometry and loss settings are unchanged, a complete original pre-daylight forecast can supply the whole view. Require a previously verified, healthy reference and complete valid native intervals from a successfully published pre-daylight issuance. Keep its original configuration, normalization capacity and provenance, and flag `reference_transition_fallback`. This selects one whole archived forecast; it does not rescale or merge bindings. Partial or invalid archives retain the latest partial outlook. Explicit forecast selection and future dates retain the current-binding policy. Evaluate today's reported outages using current knowledge; an outage declared under a different configuration remains unknown impact.

Forecast collection prepares immutable `ForecastIssuance` outputs outside database locks, then saves them atomically with regional publication under the region/job/plant/account locks. Each issuance pins the configuration, acquisition-time eligible calibration, model version, calibrated and factor-1 native power, 30/60-minute views and outage scenarios known then. Recheck the current configuration before saving; stale prepared outputs cannot attach to a new configuration. Retry publication is idempotent, and a failed transaction leaves the raw acquisition available for retry.

Policy `latest-published-pre-daylight-issuance-v1` selects the latest acquired eligible issuance for each target day. Acquisition, publication and any known provider issue time must precede the first astronomical daylight window in that binding's local day. Polar day, polar night and daylight crossing midnight use local midnight. Preparation considers the acquisition's local date and the following two dates where native intervals overlap; the collection job's canonical target date does not exclude a same-day pre-dawn update. Latest partial issuances retain their gaps rather than silently selecting an older complete forecast. Actuals, mixed outlooks and daytime forecast updates cannot replace this archive.

The plant-only `archived_forecast` source and explicit `as_of` queries read these frozen outputs without recalculation or writes. An `as_of` cutoff additionally excludes issuances not yet available then. Later model, calibration, outage or plant changes cannot alter an issuance. Mark earlier configurations explicitly. A missing issuance stays unavailable; ordinary live forecasts and retrospective estimated-actual comparisons keep their separate policies.

`collection-schedule/` exposes multiple daily region-local forecast times, the fixed night window, recent collection outcomes and the shared account's tracked UTC budget. Count successful, failed and uncertain/reserved attempts, including validation, retries and operator checks; usage outside this application is unknown. One daily request is reserved in the per-region schedule limit for history; this is a planning limit, not a guaranteed allocation across regions or a promise of unlimited night retries. The first forecast acquisition created by binding approval can run immediately outside existing clocks when collection is enabled and that schedule predates approval. Later schedule edits and disabling collection still cancel unclaimed work. Snapshot coverage indexes support history lookup, and `available_at` records atomic publication separately from acquisition. Partial history remains readable and retries create new immutable acquisitions. Saving unchanged settings preserves the effective boundary and all jobs. Forecast edits retain night jobs and queued forecast work at clocks still configured, cancel removed forecast clocks, and only generate newly added forecast clocks after the change. Disabling collection cancels unclaimed night and forecast work. An authorized in-flight response can still be archived if its binding remains current.

## Saved-forecast email reminders

Each approved plant has one shared reminder schedule with daily local `HH:MM`
times, extra recipient addresses and an English or Turkish email language. Workspace
administrators edit it; other members see its settings, recipient counts and delivery
history but no address. Optional workspace membership inclusion adds active members'
verified account addresses. Membership changes update this group automatically. An
enabled schedule needs an address or that option. Extra addresses retain their saved
value; known member addresses retain their membership identity even after an email
change. Removing a member also removes their saved addresses from current schedules.
The configuring user is nullable audit information, not the schedule owner.

The default limit is five distinct, case-insensitively compared addresses per plant,
including automatically included members,
editable through the MFA-protected admin's **Email reminder settings**. Lowering it
preserves the schedule but pauses deliveries until its list fits; each paused time is
recorded as a skipped delivery with its reason. Membership growth
above the limit also pauses delivery without truncating the group; reducing an excess
or disabling the schedule remains possible.

The current provider plan's daily request limit also caps daily reminder times; this is a product limit, not a quota reservation. Emails make **zero provider requests** and do not consume the request ledger. A reduced plan limit retains only the earliest configured times within the current limit.

The dispatcher pins the approved binding and recipient set for each slot. One durable
delivery sends one message with all recipients in BCC. Addresses remain private;
acceptance, uncertainty and retries apply to that message. An existing slot never
expands to new recipients. Before transmission, the frozen set can only shrink to
still-authorized recipients, including removal while the mail connection opens.
Pinned membership IDs and per-delivery address revocations prevent removal and
rejoining from reviving old slot authority, even when an address was originally
external. Revocations survive late mail responses and safe retries without changing
an already authorized envelope. Rejoining restores automatic inclusion in new slots.
Tenant locking serializes membership changes with the transmission permission marker;
removal after that marker cannot revoke an already authorized in-flight message, and
the history of a sent or uncertain delivery names that authorized envelope.
If collection is scheduled at the same local time, preparation waits for that exact job to finish, for at most
15 minutes. Its acquisition may become eligible within that grace period; every
other acquisition must have been collected and published by the original reminder
time. A delayed restart never admits arbitrary later acquisitions.

Select the newest eligible saved output with complete valid coverage of tomorrow
under the pinned binding. If none exists, use the newest usable partial tomorrow
output with a null complete total and explicit coverage. If neither exists, send an
unavailable notice. Today's coverage never disqualifies a valid tomorrow forecast.
Keep tomorrow's selected source. Today uses the same source composition as the screen:
completed Solcast estimated-actual intervals take priority, with saved forecasts for
the remaining hours. New acquisitions replace their coverage; older acquisitions
retain hours outside it. Wait within the existing preparation deadline for collection
jobs at the reminder's slot, without treating later intervals as completed. Pin every
contributing source and use identical native intervals for daily totals, hourly rows
and outage scenarios. Source age follows the oldest contributing acquisition.
Freeze the report for the schedule's slot and render text and HTML in the email
language pinned when scheduling. Retries preserve the frozen message and attachment
bytes. Plant messages show electrical energy in MWh, with a separate scenario for reported
outage times and capacities. Unknown outage impact remains unavailable. A compact
XLSX contains daily summaries and interval energy; dates, times and numbers are typed,
and UTC offsets disambiguate clock changes. Native intervals overlapping daylight are
retained, including missing and zero values. Hourly aggregation follows clipping and
outage limits; boundary rows retain their actual duration. Collection time, stale-source
and fallback warnings remain visible. Region-only records retain a clearly labeled
irradiance report in kWh/m². Age over 24 hours is stale.

Successful collection saves `ForecastResult.daily_payloads` for the collection's local
date and the following two dates, ending at the next local midnight, including DST boundaries, in
the same publication transaction as the canonical target-day result. Failure to
calculate a supplemental day omits only that day. Preparation calculates plant electricity
locally from the pinned saved forecast inputs, using configuration, calibration and incident
revisions eligible at the reminder slot. It freezes these results before rendering; retries never recalculate them. No provider request
is made. Validate saved dates, binding identity, finite
values, interval coverage, energy consistency and completeness before selection.
An unexpected rendering failure freezes a minimal plain-text unavailable notice,
using an independent message builder; retries preserve that same notice. Database
failures leave durable unsent work for later recovery.

Use region IANA timezones, skip nonexistent DST times and use the first occurrence
of repeated times. Catch up at most two hours; older slots are recorded without
sending a backlog. Before transmission, open the mail backend outside database
transactions, then atomically recheck the lease, deadline, approval,
current schedule, recipients and binding and record transmission permission. A worker
that lost its lease while opening the connection cannot send. Expired leases before
this marker can safely retry; after the marker they become `uncertain`, with no
automatic second send. Preserve the original token so a late known response can
resolve that same attempt.

SMTP submits DATA only after every envelope recipient accepts RCPT. Any recipient
rejection resets the transaction before content is sent, so a retry cannot duplicate
a partially accepted group. Gmail uses one API request with BCC headers. Neither
backend equates acceptance with inbox delivery.

Known non-acceptance allows at most eight attempts within the two-hour delivery
window, with exponential delay capped at 30 minutes plus 0–30 seconds of jitter.
Connection/authentication failures, explicit temporary SMTP rejection and Gmail
quota rejection are safe to retry; authentication failures retain a separate
operational error code. Permanent SMTP/API rejection fails the delivery. Invalid
SMTP replies or unconfirmed Gmail API responses cannot establish rejection and
remain uncertain. Lost acknowledgments,
disconnects or timeouts during transmission remain uncertain. A deterministic
Message-ID aids reconciliation but does not establish recipient deduplication.
`sent` means SMTP acceptance or a successful Gmail response with a message ID, not inbox
delivery; delivery, bounce and complaint tracking require a separately configured
mail provider integration. Provider and mail errors never expose credentials.
Each Django `mailers["default"]` lookup creates a fresh backend instance, so the
notification worker's threads do not share mail connections or OAuth token state.

## Primary references

- [W3C language preferences](https://www.w3.org/International/questions/qa-accept-lang-locales), [Microsoft message formatting](https://learn.microsoft.com/en-us/globalization/internationalization/message-formatting), [next-intl App Router](https://next-intl.dev/docs/getting-started/app-router), [Django translation](https://docs.djangoproject.com/en/6.1/topics/i18n/translation/).
- [Django custom administration views](https://docs.djangoproject.com/en/6.1/ref/contrib/admin/), [Cloudscape resource views](https://cloudscape.design/patterns/resource-management/view/), [GOV.UK check answers](https://design-system.service.gov.uk/patterns/check-answers/), [USWDS responsive tables](https://designsystem.digital.gov/components/table/).
- [OWASP transaction authorization](https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html), [GitLab review-context checks](https://docs.gitlab.com/api/merge_request_approvals/#approve-merge-request), [Carbon notification patterns](https://www.carbondesignsystem.com/building-blocks/core/patterns/notifications).
- [The Twelve-Factor App](https://12factor.net/), [Django design philosophies](https://docs.djangoproject.com/en/6.1/misc/design-philosophies/), [PEP 8](https://peps.python.org/pep-0008/).
- [Python 3.15.2](https://www.python.org/downloads/release/python-3152/), [Django 6.1.3](https://docs.djangoproject.com/en/6.1/releases/6.1.3/), [DRF releases](https://www.django-rest-framework.org/community/release-notes/).
- [pvlib](https://pvlib-python.readthedocs.io/en/stable/), [Dramatiq releases](https://pypi.org/project/dramatiq/) and [platform history](https://dramatiq.io/changelog.html).
- [django-allauth headless](https://docs.allauth.org/en/latest/headless/), [NIST SP 800-63B-4](https://pages.nist.gov/800-63-4/sp800-63b.html), [OWASP authentication guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html).
- [Google identity verification](https://developers.google.com/identity/gsi/web/guides/verify-google-id-token), [Microsoft identity claims](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference), [OAuth security best current practice](https://www.rfc-editor.org/rfc/rfc9700.html).
- [AWS retries and jitter](https://builder.aws.com/content/3EumjoZascWd1oZiEgL8ORlv3qE/timeouts-retries-and-backoff-with-jitter), [Google retry strategy](https://docs.cloud.google.com/storage/docs/retry-strategy), [Microsoft transient fault handling](https://learn.microsoft.com/en-us/azure/architecture/best-practices/transient-faults).
- [HTTPX transport retries](https://www.python-httpx.org/advanced/transports/), [HTTPX timeouts](https://www.python-httpx.org/advanced/timeouts/), [Dramatiq retries and time limits](https://dramatiq.io/guide.html), [Python SMTP acceptance](https://docs.python.org/3/library/smtplib.html), [Amazon SES delivery outcomes](https://docs.aws.amazon.com/ses/latest/dg/send-email-concepts-deliverability.html).
- [Django mailer construction](https://github.com/django/django/blob/stable/6.1.x/django/core/mail/handler.py), [PostgreSQL connection limits](https://www.postgresql.org/docs/current/libpq-connect.html), [PostgreSQL constraint timing](https://www.postgresql.org/docs/current/sql-set-constraints.html).
