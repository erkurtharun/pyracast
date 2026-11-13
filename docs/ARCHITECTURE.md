# Application architecture

## Outcome

Build a multi-tenant application where companies submit solar plants with their locations and technical declarations, a platform administrator links authorized Solcast accounts and approves them, and scheduled jobs publish tomorrow's irradiance and daily energy estimates. Each active location belongs to one active plant and exposes electrical generation at 30-minute or hourly resolution, including capacity limits and optional calibration against metered production. The internal Region record retains location, provider approval and collection history. The Next.js/TypeScript customer frontend lives in `web/` in this repository and runs separately from the Django API and internal admin interface.

Use only provider accounts authorized for this service. Solcast account creation remains an operator task outside this backend. Implement fixtures so development does not depend on credentials or live API calls.

## Stack and portability

- CPython **3.15.2**, Django, Django REST Framework, PostgreSQL and pvlib.
- Dramatiq with RabbitMQ is the initial worker implementation for native Windows/Ubuntu operation. Keep tasks thin so a verified better executor can replace it.
- One repository, with API, collection/calibration workers, a dedicated notification worker and scheduler/dispatcher as separate processes. Keep PostgreSQL as the durable source of truth. Queue separation alone does not reserve execution capacity for email; run its worker process independently of provider work.
- Keep frontend dependencies and their npm lockfile inside `web/`. Use Node LTS, the Next.js App Router, strict TypeScript and API types generated from the backend OpenAPI schema. Reuse the adjacent web platform's contract and verification practices without its unrelated services or deployment machinery.
- API, numerical core and worker must run on Windows x64 and Ubuntu x64. PostgreSQL and RabbitMQ may be local services, remote services or Docker Compose dependencies. Full-stack containers are an additional deployment option.
- Use portable Python entry points, `pathlib`, UTF-8, timezone-aware dates and an explicit `tzdata` dependency. Correctness must not depend on Bash, cron, Unix signals, `fork`, or development reload/watch features.

At bootstrap, verify and lock the latest stable compatible dependencies and container images. Python 3.15.2 is fixed; this document is **not a tested compatibility matrix**. Verify transitive scientific dependencies, installation, migrations, an API request, a pvlib calculation and a real queued task on both operating systems. If blocked, report the exact incompatibility and smallest alternative; continue independent work.

Celery has no supported native Windows runtime. Selecting Celery would require an explicit Linux-container/WSL worker contract; it must not be presented as native Windows support.

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
collect; `PYRACAST_LOG_LEVEL` controls verbosity and defaults to `INFO`. Local launcher
logs and process identities are development tooling.

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
The production HTTPS gateway and process supervision remain deployment responsibilities.

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
| EpiasConnection, EpiasCollectionJob, EpiasSnapshot | Plant identity, independent collection and immutable reported-production acquisitions |
| CalibrationRun, CalibrationActivation | Durable fit, reproducible dataset, held-out evaluation and audited manual/automatic activation history |
| HistoricalReferenceImport | Authorized source settings, immutable export digest and actual acquisition time |
| ReferenceCapacityNormalization | Authorized capacity-only copies, original source lineage and conversion availability |
| PlantForecast, PlantEvent | Immutable electrical result and audited plant operations |
| NotificationSettings, ReminderPreference, NotificationDelivery | Shared plant recipient limit, per-user email schedules, pinned saved forecasts and per-recipient delivery outcomes |
| AuditEvent | Actor, action, target and timestamp for privileged changes |

Initially each region uses a separately configured provider account. Keep the relationship explicit rather than embedding a credential in the region. Enforce one canonical record per provider account across tenants and credential rotations; duplicate records must not create independent quota budgets.

Regions do not store or expose a surface setting: the irradiance result identifies
its horizontal plane. Provider bindings store one `power_basis` for their matched
power and normalization capacity. The numerical boundary still validates
independently declared bases, and archived source manifests retain their fields.

Forecast snapshots pin their job's binding under a composite foreign key, supporting source lookup and a database-enforced normalization target. Normalization retains its unique source/target binding key for idempotency.

## Approval flow

Company registration creates a non-staff user, a new tenant and its initial membership
in one transaction. It never joins an existing company by name or email. The
django-allauth browser API owns registration, email verification, login, recovery,
reauthentication, MFA and session management. Email ownership is mandatory before
login. Django validates passwords with a 12-character minimum and common-password
screening, hashes new passwords with Argon2 and rotates session identifiers on login.
Mutations enforce CSRF before authentication. Allauth's account and IP rate limits
share an atomic PostgreSQL JSON/TTL cache; ingress limits additionally protect capacity.

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

1. A tenant member submits a plant, coordinates, IANA timezone and technical declaration together through `sites/`; the location and plant commit atomically with state `pending`. Customer submissions never contain provider credentials. Existing locations without a plant can complete setup through the scoped plant endpoint.
2. A platform admin records the authorized provider account and site binding. The backend validates credentials and site settings within that account's quota.
3. One shared approval operation checks permissions and the validated configuration revision, then atomically records `approved`, materializes the plant's immutable binding-backed configuration, records an audit event and creates the first pending collection job. A pending plant's technical declaration remains editable until approval; the approved declaration remains available afterwards while subsequent technical changes create configuration revisions.
4. A dispatcher delivers that durable job to the worker. The worker retrieves data, calculates and publishes a result.
5. The frontend reads saved results and their freshness. It does not call Solcast.

Keep approval (`pending`, `approved`, `rejected`, `suspended`), connection health and data freshness separate. Approval does not mean a forecast is ready. Coordinate or model-setting changes create a new revision requiring validation and approval; stale jobs must not publish into the new revision.

PostgreSQL permits one active plant per region and one active location per tenant, comparing latitude and longitude rounded to six decimal places. The application uses the same rounding for new coordinates. The same location in another tenant is independent. Deleted definitions and their immutable archives remain intact; restoring a definition rejects an active replacement conflict. Migrations add constraints without deleting or merging existing records and require conflicting legacy active definitions to be resolved explicitly before migration. Provider reapproval creates a new configuration revision from the current technical parameters when the binding changes, preserving prior configuration and source identities.

Members can rename a region without changing its revision or approval. Location changes are reviewed requests pinned to the current revision; one pending request per region is allowed. Administrator acceptance applies a new pending revision and removes the old approval. A replacement binding must be configured, validated and approved before collection resumes. Rejection preserves the current location.

## Reliability and security

- Keep one authentication authority: django-allauth browser sessions, with no app-token endpoints or separate password login. Platform admin and API actions require active superuser status and MFA evidence bound to the current user and a live authenticator. Administrators require a TOTP factor provisioned through the trusted operator command before login; password possession cannot bootstrap an administrative factor. Upgrades remove an empty legacy OTP table but stop if any legacy device exists, requiring verified factor migration instead of reducing MFA to password/email enrollment. Reauthentication protects security changes, and recovery codes are one-use and shown once. MFA secrets use authenticated encryption with an HKDF context distinct from provider credentials.
- Derive tenant access from authenticated membership. Scope list/detail/create/update operations, worker jobs and cache keys. Tenant ownership is not a trusted request field. Django admin does not provide tenant isolation automatically.
- Store provider keys in a secret manager or authenticated encryption with keys outside the database. Accept credential changes through write-only admin inputs; redact secrets from responses, logs and audit payloads.
  The MFA-protected administration setup page supports existing authorized accounts
  or a new private account, followed by explicit connection validation and approval.
  Pasted keys use Fernet authenticated encryption, with an HKDF-SHA256 key derived
  from the externally configured Django secret and a versioned application context.
  Solcast inputs are write-only raw API keys; persistence accepts encrypted credentials
  and never resolves environment references. Existing-account key changes require
  explicit confirmation and invalidate validation for every binding on that account
  without replacing the canonical identity or resetting quota. Migration preserves
  encrypted credentials and rejects unresolved legacy Solcast references. Key rotation
  can decrypt with configured secret-key fallbacks; `reencrypt_secrets` rewrites stored
  credentials under the new primary key before the old fallback is removed. Back up
  the external keys separately from the database. This protects database-only
  disclosure, not compromise of the application host that holds the decryption key.
- Reserve quota atomically by provider account and UTC day before each request, including validation and retries. Uncertain request outcomes consume budget conservatively; do not rotate accounts to evade limits. Solcast HTTP 429 records a shared account cooldown; HTTP 401 blocks new requests for the failing credential revision until key rotation. These gates stop request admission without disabling access to previously authorized saved forecasts. HTTP 403 is a permanent failure of that request, not an account-wide credential verdict.
  Credential rotation preserves account cooldowns. A 429 without a usable retry
  hint waits at least 15 minutes. An audited administrative reset requires the
  operator to confirm provider recovery and changes neither request usage nor
  authentication state. Successful validation can re-arm the latest approved
  forecast job that failed authentication, without clearing its request ledger.
- PostgreSQL owns retry scheduling, attempt budgets and leases. Dramatiq actors retain `max_retries=0`; HTTP adapters make one ledger-accounted request per attempt. Retry transport failures, HTTP 408/425/429 and transient 5xx responses; other HTTP failures, including 501/505, require a later configuration or service change. Preserve safe error codes and `Retry-After`, accepting delta seconds or an HTTP date as a minimum delay without shortening long hints. Add 0–30 seconds of jitter to bounded exponential backoff. Forecast jobs allow three execution attempts, including interrupted lease recovery; admission waits consume neither a request nor an execution attempt. Transient forecast-provider failures wait at least four and then eight minutes, so a five-minute outage can recover within the 15-minute preparation grace without increasing the three-request budget; calculation failures retain separate backoff. A newer configured forecast slot supersedes older unclaimed work, preventing an outage from replaying an obsolete backlog. Account quota and forecast reservations still apply to every actual retry.
- Claim jobs with expiring leases and unique scheduling keys. Assume queue redelivery: persist results idempotently and recover interrupted work. Publish through a durable job/outbox record so a database commit cannot lose the collection request. Keep HTTP connection/read/write/pool limits distinct from job and business deadlines: HTTPX timeouts bound individual I/O waits, and Dramatiq's best-effort time limit is not a hard network deadline.
- Members configure region-local daily forecast times, initially 16:00. Historical collection runs separately at 03:00, refreshing yesterday and recovering missed days from the available seven-day window. Request `hours=168` explicitly and archive completed intervals; each acquisition remains immutable. Historical transport failures, empty responses and incomplete previous days retry no sooner than 30 minutes later, before 09:00. Restart catch-up is allowed inside that night window; disabling collection stops new night work. Account-wide UTC quota reservations preserve the remaining scheduled forecast needs before admitting a night request. Quota exhaustion or reserved forecast budget never creates a rapid retry loop. Configuration changes apply to future forecast slots; they do not invent missed forecast acquisitions. Use unique slots, advance a nonexistent night start to the first valid half-hour before 09:00, choose the first repeated time and flag late acquisitions.
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
SMTP sockets use a 15-second timeout. These are connection and individual
operation limits; business deadlines are checked separately after database waits.

`DispatcherStatus` records each phase's last attempt, success and safe error code.
`check_delivery_health --max-lag-minutes 5` emits JSON and fails for missing, stale
or failed phases, overdue ready jobs, expired or missing active leases, recent
failed/uncertain/unavailable or incomplete tomorrow deliveries, missed delivery
deadlines, EPİAŞ failures, credential blocks, account cooldowns and mail
authentication failures. Enabled approved regions without a successful forecast
in 26 hours are unhealthy. Complete saved-forecast fallback remains informational.
This check needs a working database; monitoring
must also treat failure to execute it as unhealthy. Run the mailer with
`worker --queues notifications --threads 2`, separately from the default collection,
calibration and EPİAŞ queues. Restart all workers during rollout: an older worker
continues consuming all queues until stopped.

## Database invariants and rollout

Use Django migrations as the sole schema owner. The migration dependency graph,
historical models and `django_migrations` ledger travel with the application.
Applied operations and data transformations are immutable; fixes use new forward
migrations. Historical model type annotations can be corrected without changing
replay behavior. Do not add Flyway
or Liquibase over the same tables. `makemigrations --check --dry-run` checks model
state against committed migrations, while `migrate --check` checks unapplied
migrations; neither is a physical-schema drift detector or a migration checksum.
Do not make unrecorded manual DDL changes.

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
New CHECK and composite FK constraints are added without scanning old rows, then
validated in separate migrations; deployment is incomplete until validation passes.
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
change. Production recovery additionally needs retention, off-host backups,
monitored WAL archiving/PITR and a tested recovery objective. Back up external
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

EPİAŞ production uses a separate `metering` app and durable `epias` queue. A tenant
member links a plant by provider ID and EIC; server-side `EPIAS_USERNAME` and
`EPIAS_PASSWORD` obtain a cached TGT without exposing credentials to the browser.
Collect 90 completed Istanbul dates of reported production on connection or a new plant configuration,
then reconcile the trailing seven dates at the region's independent 03:00 nightly
slot. The previous day's reported-production job retries incomplete or failed
responses every 30 minutes before 09:00, respecting longer provider retry hints.
Older correction dates and settlement jobs retain three attempts. All new daily
jobs stop at the morning deadline or when collection is disabled; initial backfill
remains independent. Resolve the separate settlement plant ID by EIC and initially collect
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

The plant's **Automatic production** tab keeps provisional reported production and
settlement injection selectable separately. Automatic selection prefers valid
UEVM values hour by hour, falling back to the latest reported-production acquisition
with its provisional label. Preserve
native hourly labels, MWh, missing values, collection/publication times and a
payload digest. Policy `hour-start-istanbul-v1` treats 10:00 as [10:00, 11:00).
Missing provider rows have null values and an explicit `reported: false` flag.
Latest revisions never fill their missing hours from older acquisitions.

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
The entry journal instead preserves available reference slots and saved/draft
measurements. Numerical views offer UTF-8 CSV downloads containing the entire
selected dataset, including hidden edge intervals, units and provenance. CSV cells
preserve missingness, precision and quoting, and neutralize spreadsheet formulas
in text fields.

Provide authenticated region submission/list/detail, platform-admin connection validation and approve/reject/suspend actions, and dated forecast retrieval. Generate OpenAPI; use explicit units, bounded date ranges, pagination and stable error codes. Approval transitions are operations, not writable status fields.

Members can move their region or plant definitions to a tenant-scoped recycle bin. Keep data for 30 elapsed days from the first deletion; repeated deletion never extends the deadline. Normal reads and writes hide deleted definitions, and region deletion also hides its children and stops collection, calibration and reminders. Restoring a region preserves approval and configuration, resumes future schedules without catch-up, and leaves independently deleted plants in the bin. A plant can only be restored under an active region. Already submitted email cannot be recalled.

The dispatcher permanently purges expired definitions and their owned records in bounded transactional batches. Restore is unavailable at the deadline even if a purge batch is pending. Retain shared provider accounts and account request-attempt counters without deleted region/job links, so deletion cannot reset quota. Preserve historical data during the recovery period; deleting a definition is separate from replacing a forecast vintage.

Serve the frontend, `/api/`, `/admin/` and Django's `/static/` assets through one
origin. The Next.js development server proxies Django routes and assets; the
production gateway owns this routing. Django owns authentication and
authorization; the allauth browser API enforces CSRF even before login. Sessions
use HttpOnly, SameSite=Lax cookies, with Secure and a `__Host-` prefix in production.
Revoke sessions after 12 hours or 30 minutes without requests; automatic polling
counts as activity. Sessions predating the lifetime markers fail closed. API
responses are private and non-cacheable. The frontend reads stored results and
never handles provider credentials. A fixed public HTTPS origin determines
security-email navigation. Verification and recovery codes expire after ten minutes,
allow at most three attempts and are bound to the initiating browser session. Real
SMTP delivery is a production requirement, separate from mocked mail validation.
Local development may explicitly select Django's console backend with
`PYRACAST_EMAIL_BACKEND=console` and `PYRACAST_DEBUG=1`; messages, including usable
verification codes, go to private local process logs. Console mail in production or
an unknown backend stops startup. Email replacements require reauthentication
and verification; password changes invalidate other sessions. Administrator recovery
code access and regeneration require explicit MFA reauthentication within five minutes,
bound to a still-enrolled factor; password reauthentication is insufficient.

Requests sharing a valid session cookie serialize through a PostgreSQL advisory
lock around session loading and response saving. This prevents concurrent code
attempts or ordinary polling from overwriting security state. Acquisition waits
at most five seconds before returning HTTP 429 with `Retry-After`; provider calls
do not hold a database transaction. The default database connection must remain
on one PostgreSQL session throughout the request: use a direct connection or an
external session pool, never transaction-pooled PgBouncer. Django's built-in
connection pool is rejected. Connection loss fails closed and releases the lock.

Forwarded headers are accepted only from explicitly configured immediate proxy
CIDRs. The gateway must overwrite the scheme and single client IP, while direct
untrusted peers cannot supply either identity. Authentication rate limits use
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

Weather supplier identity and account/site details remain internal. Customer JSON, email and CSV use neutral source labels while preserving units, missingness, source kinds and quality warnings. Delivery projections never rewrite saved provenance. The customer OpenAPI schema excludes platform operations and their configuration components; platform endpoints retain administrator authorization.

Auxiliary Open-Meteo weather is the narrow attribution exception: its license requires visible source and license links next to displayed data. Worker forecast deliveries archive its native hourly GHI, temperature endpoints and ground-snow indicators separately on the Hobby forecast snapshot, without using additional Solcast endpoints or quota. Regional results expose an independent weather reference with its own energy, coverage, collection time and attribution; weather failure does not prevent the Hobby result from publishing. Neither source fills the other's gaps. Existing emails and CSV exports retain their original dataset. The optional customer API key lives only in environment configuration; the free endpoint is for evaluation/non-commercial use.

Plant operations use the same tenant membership boundary. Configuration revisions pin the approved reference binding and distinguish DC nameplate, inverter AC capacity, grid export limit and the measurement point (`net_export` or `ac_generation`). Unknown capacities remain null. Changing the configuration supersedes pending calibration work; it does not rewrite historical forecasts or observations.
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

Without a reported outage, a live outlook can add `derating_scenario` when the latest
completed calibration found recent strong, unclipped production below the active factor.
It scales remaining capacity by the measured ratio next to the normal outlook, expires
ten days after its evidence window and yields to any reported outage. Automatic promotion
over an active calibration pauses while it is suspected. Details are in `IRRADIANCE.md`.

The production journal selects one completed local day at 30/60-minute resolution and shows whole meter intervals overlapping astronomical daylight. Astral calculates a sea-level horizon from immutable binding coordinates and timezone without provider requests. Previous/next controls preserve unsaved entries in memory; saved observations, including night and previous configurations, remain in a separate expandable list. The API retains explicit full-day/range access for integrations. New meter entries and CSV imports require complete matching archived estimated-actual coverage and the current approved plant configuration; forecasts do not unlock entry. The available window grows with the archive. Saved readings remain visible after configuration changes. Import energy in explicit MWh or kWh, with timezone-aware bounds and quality labels. Exact replays are idempotent; conflicting or overlapping records reject the entire batch. PostgreSQL exclusion constraints enforce non-overlap, requiring the `btree_gist` extension. An import contains at most 2,000 rows; larger histories use multiple batches. Observations are immutable in this version; corrections need an explicit replacement-history design before they can be supported safely.

Saving production automatically prepares a durable calibration job once at least seven days have complete, valid matched daylight coverage within a 180-day pool ending on the latest recorded local day. With enough development history, the fitter chooses a 30/60/90-day window on inner chronological blocks before the final untouched holdout. Additional readings can be saved while a job is running; terminal completion prepares one follow-up if new eligible data arrived. An unchanged dataset does not trigger repeated jobs. The manual API retains its 7–90 completed-day range for operator compatibility. A separate Dramatiq `calibration` queue performs offline fitting without provider calls; leases expire after five minutes and permit at most three attempts. Pin observation creation time and ID watermark, source snapshots, configuration, selection policy and a dataset digest. Recheck configuration and lease ownership before publication. Authorized members can enable daily automatic checks and guarded promotion before any fit is ready. Promotion additionally requires current Hobby source validation, baseline/incumbent improvement and seven new holdout days after the previous promotion. Disabling keeps the last good selection; baseline reset disables automation, and configuration changes clear its authorization. Activations are audited separately from immutable fits, with a null actor for system decisions and the authorizing member in event details. Details and statistical acceptance criteria live in `IRRADIANCE.md`.

Historical rooftop PV exports enter through an admin command with an explicit settings manifest matching the approved binding. Canonical input paths remain inside the trusted working directory. Store the original digest, native interval means, product/settings and real import time; repeated imports are idempotent and corrections create new acquisitions. The entire archive commits before one calibration preparation. It consumes no Hobby quota. Historical and native Hobby estimated actuals remain different products: fitting on imported history alone cannot authorize automatic live promotion. Retrospective physical configuration assumptions remain visible in the fit manifest.

The current/future plant outlook uses `latest-forecast-coverage-v2` for the current approved configuration. Each published forecast owns its coverage window in newest-first order; older forecasts fill only time outside newer coverage, so an intraday update does not discard the morning. Nulls, missing rows and zeros inside newer coverage remain authoritative. Record source snapshots, coverage segments, native interval bounds and per-output interval source IDs. Both normal and outage scenarios use the same composition; freshness follows the oldest contributing acquisition. Collection at each configured time refreshes this outlook, including intraday updates; open forecast screens refresh saved results every 30 seconds without provider requests. Historical electrical forecasts and explicit `as_of` replays retain the first eligible forecast acquired on the preceding local day, excluding acquisitions and activations not yet available then. Calibration instead pairs production with complete native Hobby estimated actuals when available, otherwise complete historical acquisitions, applying the request cutoff before source ranking; it never falls back to forecasts. Preserve immutable acquisitions, source kinds and publication times. Later revisions cannot change a completed fit or an archived forecast. Missing history yields insufficient calibration data.
Calibration jobs pin the incumbent activation under the plant lock. An eligible candidate must satisfy the current numerical policy and cannot replace a different activation selected after fitting was requested. Restore-to-baseline remains explicit. The performance API and frontend compare historical day-ahead input replays to meter readings for the current configuration. The frontend uses daylight scope; the API also retains explicit full-day evaluation, with missing days and publication cutoffs exposed; the statistical contract is in `IRRADIANCE.md`.

Production comparison opens the separate `historical_model` mode: saved estimated actuals and reported production under the current configuration with factor 1. Retrospective configuration assumptions and provisional production remain labeled. Archived forecast evaluation stays separately selectable. Show matched days in the table, summarize unavailable dates in a disclosure and use a compact empty state when no pairs exist.

The plant screen opens tomorrow in the region's timezone at 60-minute resolution. Its ordinary automatic source selects Solcast estimated actuals for completed past days, and forecasts for today and future days. Missing past-day history returns an unavailable estimated-actual result. Ordinary past-day actuals use the last configuration known at local day start, or the last declaration during that day if none existed at its start. If no configuration existed that day, use the first known declaration as an explicitly assumed retrospective estimate. Later binding revisions do not hide the selected day's archived source. Explicit `as_of` and historical-model performance comparisons retain their separate selection rules. Explicit source selection preserves access to both series; the API defaults to forecasts for compatibility. A missing configuration or source returns `freshness: missing`, a null result and `unavailable_reason`. Historical electrical estimates use factor 1 and label retrospective configuration assumptions; they are not metered output. Configuration revisions are never backdated to fill a gap.

For today's ordinary automatic view, an intraday reference-capacity change can leave the current binding with only evening coverage. If the physical plant parameters and the reference account/site, location, timezone, geometry and loss settings are unchanged, a complete original day-ahead forecast can supply the whole view. Require a previously verified, healthy reference and complete valid native intervals from a successfully published pre-day-start vintage. Keep its original configuration, normalization capacity and provenance, and flag `reference_transition_fallback`. This selects one whole archived forecast; it does not rescale or merge bindings. Partial or invalid archives retain the latest partial outlook. Explicit forecast selection and future dates retain the current-binding policy. Evaluate today's reported outages using current knowledge; an outage declared under a different configuration remains unknown impact.

The plant-only `archived_forecast` source displays a saved day-ahead forecast for today or a completed day, using the configuration known at that day's start. It preserves the original binding, scale, source publication cutoff and eligible calibration. Read an existing matching cached output or calculate a read-only projection without creating a new archived forecast. Label previous configurations and assumed first declarations explicitly; retrospective configuration assumptions use factor 1. Explicit `as_of` never assumes a configuration declared later. The normal current/future outlook continues to use the current approved binding.

When a plant was first configured after the selected day began, the ordinary outlook can project that day's archived forecast using the current approved configuration and factor 1. Mark `historical_configuration_assumed`, retain the original day-ahead source cutoff and record configuration creation time. This is a temporary projection, not a new archived plant forecast. Explicit `as_of` queries remain strict; existing historical configurations and calibrated forecasts keep their original selection rules.

`collection-schedule/` exposes multiple daily region-local forecast times, the fixed night window, recent collection outcomes and the shared account's tracked UTC budget. Count successful, failed and uncertain/reserved attempts, including validation, retries and operator checks; usage outside this application is unknown. One daily request is reserved in the per-region schedule limit for history; this is a planning limit, not a guaranteed allocation across regions or a promise of unlimited night retries. The first forecast acquisition created by binding approval can run immediately outside existing clocks when collection is enabled and that schedule predates approval. Later schedule edits and disabling collection still cancel unclaimed work. Snapshot coverage indexes support history lookup, and `available_at` records atomic publication separately from acquisition. Partial history remains readable and retries create new immutable acquisitions. Saving unchanged settings preserves the effective boundary and all jobs. Forecast edits retain night jobs and queued forecast work at clocks still configured, cancel removed forecast clocks, and only generate newly added forecast clocks after the change. Disabling collection cancels unclaimed night and forecast work. An authorized in-flight response can still be archived if its binding remains current.

Acceptance: a second tenant cannot access the first tenant's records; concurrent approvals/jobs preserve invariants; retries respect quota; restart/redelivery does not duplicate results; invalid secrets remain private; incomplete days are not reported as zero; both OS environments pass the implemented flows. Without live credentials or matched labels, finish the fixture-backed system and identify the unverified integration/accuracy boundary.

## Saved-forecast email reminders

Each member can configure daily local `HH:MM` times and a recipient list for an approved plant. An empty list uses the member's account email. Active members' saved lists share one plant-wide limit of five distinct, case-insensitively compared addresses by default, editable through the MFA-protected admin's **Email reminder settings**. Paused lists retain their places; clearing the addresses and times while paused releases them. Inactive users and departed members do not reserve places. The API returns aggregate counts without exposing another member's addresses. A lower limit preserves saved lists but pauses the plant's deliveries until the aggregate fits; edits that reduce an existing excess remain possible. Requests require a `recipients` list, and `effective_recipients` resolves an empty list to the account email. Store only the recipient list. Migration `notifications.0005` preserves legacy-only addresses before removing the redundant email column; its reverse recreates the single-recipient mirror, not obsolete shadow values that disagreed with a nonempty canonical list.

The current provider plan's daily request limit also caps daily reminder times; this is a product limit, not a quota reservation. Emails make **zero provider requests** and do not consume the request ledger. A reduced plan limit retains only the earliest configured times within the current limit.

The dispatcher pins the approved binding and complete recipient set for each slot.
Each address gets its own message and durable delivery record, so addresses remain
private and failures or retries stay independent. An existing slot never expands to
new recipients, including after an account email change. If collection is scheduled
at the same local time, preparation waits for that exact job to finish, for at most
15 minutes. Its acquisition may become eligible within that grace period; every
other acquisition must have been collected and published by the original reminder
time. A delayed restart never admits arbitrary later acquisitions.

Select the newest eligible saved output with complete valid coverage of tomorrow
under the pinned binding. If none exists, use the newest usable partial tomorrow
output with a null complete total and explicit coverage. If neither exists, send an
unavailable notice. Today's coverage never disqualifies a valid tomorrow forecast.
Freeze the selected source and rendered text, HTML and CSV for the whole plant slot
before sending; retries and other recipients use that same content. Source age,
fallback kind, collection time, units and completeness remain visible. Age over
24 hours is stale; a stale source is usable only for dates it actually covers.
These messages describe incident irradiance energy in kWh/m², separately from plant
electrical generation in MWh.

Successful collection saves `ForecastResult.daily_payloads` for the local dates
covered by its elapsed 72-hour acquisition horizon, including DST boundaries, in
the same publication transaction as the canonical target-day result. Failure to
calculate a supplemental day omits only that day. Email rendering performs no
model recomputation or provider request, so a later solver failure cannot destroy
previously saved usable outputs. Validate saved dates, binding identity, finite
values, interval coverage, energy consistency and completeness before selection. Migration
`forecasts.0017` copies only an existing result's matching original target day;
additional historical days are not recalculated or backdated. The wider fallback
window becomes available after subsequent successful collections.
An unexpected rendering failure freezes a minimal plain-text unavailable notice,
using an independent message builder; retries preserve that same notice. Database
failures leave durable unsent work for later recovery.

Use region IANA timezones, skip nonexistent DST times and use the first occurrence
of repeated times. Catch up at most two hours; older slots are recorded without
sending a backlog. Before transmission, open the mail connection outside database
transactions, then atomically recheck the lease, deadline, membership, approval,
preferences, recipient and binding and record transmission permission. A worker
that lost its lease while opening the connection cannot send. Expired leases before
this marker can safely retry; after the marker they become `uncertain`, with no
automatic second send. Preserve the original token so a late known response can
resolve that same attempt. Legacy interrupted deliveries without prepared content
remain uncertain because their transmission phase is unknown. Migration
`notifications.0006` conservatively marks existing sending/uncertain rows using
their saved acceptance time or scheduled time; this legacy marker is not an
observed SMTP transmission timestamp.

Known non-acceptance allows at most eight attempts within the two-hour delivery
window, with exponential delay capped at 30 minutes plus 0–30 seconds of jitter.
Connection/authentication failures and explicit temporary SMTP rejection are safe
to retry; authentication failures retain a separate operational error code.
Permanent transaction rejection fails the delivery. Invalid SMTP reply codes
cannot establish rejection and remain uncertain. Lost acknowledgments,
disconnects or timeouts during transmission remain uncertain. A deterministic
Message-ID aids reconciliation but does not establish recipient deduplication.
`sent` means the configured mail backend acknowledged acceptance, not inbox
delivery; delivery, bounce and complaint tracking require a separately configured
mail provider integration. Provider and mail errors never expose credentials.
Each Django `mailers["default"]` lookup creates a fresh backend instance, so the
notification worker's threads do not share an SMTP connection.

## Primary references

- [Django custom administration views](https://docs.djangoproject.com/en/6.1/ref/contrib/admin/), [Cloudscape resource views](https://cloudscape.design/patterns/resource-management/view/), [GOV.UK check answers](https://design-system.service.gov.uk/patterns/check-answers/), [USWDS responsive tables](https://designsystem.digital.gov/components/table/).
- [The Twelve-Factor App](https://12factor.net/), [Django design philosophies](https://docs.djangoproject.com/en/6.1/misc/design-philosophies/), [PEP 8](https://peps.python.org/pep-0008/).
- [Python 3.15.2](https://www.python.org/downloads/release/python-3152/), [Django 6.1.3](https://docs.djangoproject.com/en/6.1/releases/6.1.3/), [DRF releases](https://www.django-rest-framework.org/community/release-notes/).
- [pvlib](https://pvlib-python.readthedocs.io/en/stable/), [Dramatiq releases](https://pypi.org/project/dramatiq/), [Dramatiq platform history](https://dramatiq.io/changelog.html), [Celery Windows limitation](https://docs.celeryq.dev/en/stable/faq.html#does-celery-support-windows).
- [django-allauth headless](https://docs.allauth.org/en/latest/headless/), [NIST SP 800-63B-4](https://pages.nist.gov/800-63-4/sp800-63b.html), [OWASP authentication guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html).
- [AWS retries and jitter](https://builder.aws.com/content/3EumjoZascWd1oZiEgL8ORlv3qE/timeouts-retries-and-backoff-with-jitter), [Google retry strategy](https://docs.cloud.google.com/storage/docs/retry-strategy), [Microsoft transient fault handling](https://learn.microsoft.com/en-us/azure/architecture/best-practices/transient-faults).
- [HTTPX transport retries](https://www.python-httpx.org/advanced/transports/), [HTTPX timeouts](https://www.python-httpx.org/advanced/timeouts/), [Dramatiq retries and time limits](https://dramatiq.io/guide.html), [Python SMTP acceptance](https://docs.python.org/3/library/smtplib.html), [Amazon SES delivery outcomes](https://docs.aws.amazon.com/ses/latest/dg/send-email-concepts-deliverability.html).
- [Django mailer construction](https://github.com/django/django/blob/stable/6.1.x/django/core/mail/handler.py), [PostgreSQL connection limits](https://www.postgresql.org/docs/current/libpq-connect.html), [PostgreSQL constraint timing](https://www.postgresql.org/docs/current/sql-set-constraints.html).
