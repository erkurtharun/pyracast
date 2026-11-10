# Application architecture

## Outcome

Build a multi-tenant application where customers request regions, a platform administrator links authorized Solcast accounts and approves them, and scheduled jobs publish tomorrow's irradiance and daily energy estimates. Plants belong to regions and expose electrical generation at 30-minute or hourly resolution, including capacity limits and optional calibration against metered production. The Next.js/TypeScript customer frontend lives in `web/` in this repository and runs separately from the Django API and internal admin interface.

Use only provider accounts authorized for this service. Solcast account creation remains an operator task outside this backend. Implement fixtures so development does not depend on credentials or live API calls.

## Stack and portability

- CPython **3.15.2**, Django, Django REST Framework, PostgreSQL and pvlib.
- Dramatiq with RabbitMQ is the initial worker implementation for native Windows/Ubuntu operation. Keep tasks thin so a verified better executor can replace it.
- One repository, with API, worker and scheduler/dispatcher as separate processes. Keep PostgreSQL as the durable source of truth.
- Keep frontend dependencies and their npm lockfile inside `web/`. Use Node LTS, the Next.js App Router, strict TypeScript and API types generated from the backend OpenAPI schema. Reuse the adjacent web platform's contract and verification practices without its unrelated services or deployment machinery.
- API, numerical core and worker must run on Windows x64 and Ubuntu x64. PostgreSQL and RabbitMQ may be local services, remote services or Docker Compose dependencies. Full-stack containers are an additional deployment option.
- Use portable Python entry points, `pathlib`, UTF-8, timezone-aware dates and an explicit `tzdata` dependency. Correctness must not depend on Bash, cron, Unix signals, `fork`, or development reload/watch features.

At bootstrap, verify and lock the latest stable compatible dependencies and container images. Python 3.15.2 is fixed; this document is **not a tested compatibility matrix**. Verify transitive scientific dependencies, installation, migrations, an API request, a pvlib calculation and a real queued task on both operating systems. If blocked, report the exact incompatibility and smallest alternative; continue independent work.

Celery has no supported native Windows runtime. Selecting Celery would require an explicit Linux-container/WSL worker contract; it must not be presented as native Windows support.

## Boundaries and records

Use conventional Django apps for accounts, regions, provider connections, forecasts and plants; keep calculations independent of Django. API views, admin actions and tasks call shared application operations. Avoid business logic in serializers or implicit model signals.

| Record | Owns |
| --- | --- |
| Tenant, User, Membership | Customer ownership and permitted actions |
| Region | Tenant, coordinates, IANA timezone, surface and approval state |
| ProviderAccount | Provider identity, secret reference, entitlement and account-wide quota |
| SiteBinding | Region-to-provider-account/site mapping and immutable configuration revisions |
| CollectionJob, RequestAttempt | Scheduling, leases, quota reservations and execution history |
| ForecastSnapshot, ForecastResult | Raw input, normalized intervals, derived output and model version |
| Plant, PlantConfiguration | Region ownership and immutable DC/inverter/export capacity and meter-basis revisions |
| ProductionObservation | Interval energy, quality, source, configuration and recording time |
| CalibrationRun, CalibrationActivation | Durable fit, reproducible dataset, held-out evaluation and explicit activation history |
| PlantForecast, PlantEvent | Immutable electrical result and audited plant operations |
| ReminderPreference, NotificationDelivery | Per-user region email schedules, pinned saved forecasts and durable delivery outcomes |
| AuditEvent | Actor, action, target and timestamp for privileged changes |

Initially each region uses a separately configured provider account. Keep the relationship explicit rather than embedding a credential in the region. Enforce one canonical record per provider account across tenants and credential rotations; duplicate records must not create independent quota budgets.

## Approval flow

1. A tenant member submits a region; its state is `pending`.
2. A platform admin records the authorized provider account and site binding. The backend validates credentials and site settings within that account's quota.
3. One shared approval operation checks permissions and the validated configuration revision, then atomically records `approved`, an audit event and the first pending collection job.
4. A dispatcher delivers that durable job to the worker. The worker retrieves data, calculates and publishes a result.
5. The frontend reads saved results and their freshness. It does not call Solcast.

Keep approval (`pending`, `approved`, `rejected`, `suspended`), connection health and data freshness separate. Approval does not mean a forecast is ready. Coordinate or model-setting changes create a new revision requiring validation and approval; stale jobs must not publish into the new revision.

## Reliability and security

- Authenticate with maintained Django mechanisms; use secure sessions and CSRF protection for browser access unless an existing identity provider requires another supported integration. Restrict platform-admin actions and protect admin access with MFA.
- Derive tenant access from authenticated membership. Scope list/detail/create/update operations, worker jobs and cache keys. Tenant ownership is not a trusted request field. Django admin does not provide tenant isolation automatically.
- Store provider keys in a secret manager or authenticated encryption with keys outside the database. Accept credential changes through write-only admin inputs; redact secrets from responses, logs and audit payloads.
- Reserve quota atomically by provider account and UTC day before each request, including validation and retries. Use a shared database ledger, bounded timeouts and retry backoff. Uncertain request outcomes consume budget conservatively; do not rotate accounts to evade limits.
- Claim jobs with expiring leases and unique scheduling keys. Assume queue redelivery: persist results idempotently and recover interrupted work. Publish through a durable job/outbox record so a database commit cannot lose the collection request.
- Members configure region-local daily collection times, initially 16:00. Each slot retrieves forecasts; the first eligible slot also retrieves estimated actuals, refreshing yesterday and recovering missed days from the available seven-day window. Request `hours=168` explicitly and archive completed intervals; each acquisition remains immutable. Both requests share the account's UTC quota with validation and retries. Configuration changes apply to future slots; they do not invent missed acquisitions. Use unique slots, skip DST gaps, choose the first repeated time and flag late acquisitions.
- Do not hold database transactions open during external HTTP calls. Recheck binding revision and approval before publication. Multiple scheduler instances must not duplicate work.
- Preserve forecast vintages and configuration/model versions. Expose stale or incomplete results explicitly. Monitor last successful collection, queue lag, provider errors, quota, solver failures and coverage; logs carry correlation IDs without credentials.

## API and delivery

Provide authenticated region submission/list/detail, platform-admin connection validation and approve/reject/suspend actions, and dated forecast retrieval. Generate OpenAPI; use explicit units, bounded date ranges, pagination and stable error codes. Approval transitions are operations, not writable status fields.

Serve the frontend and `/api/` through one origin. Django owns authentication and authorization; JSON session endpoints enforce CSRF even before login. Keep sessions in HttpOnly cookies and API responses private and non-cacheable. The frontend reads stored results and never handles provider credentials. Forwarded HTTPS headers are trusted only when explicitly enabled behind a private, trusted proxy. Rate-limit authentication at the deployment ingress.

Plant operations use the same tenant membership boundary. Configuration revisions pin the approved reference binding and distinguish DC nameplate, inverter AC capacity, grid export limit and the measurement point (`net_export` or `ac_generation`). Unknown capacities remain null. Changing the configuration supersedes pending calibration work; it does not rewrite historical forecasts or observations.

The production journal browses up to 31 local days at 30/60-minute resolution, showing saved readings and missing intervals. New meter entries and CSV imports require complete matching archived estimated-actual coverage and the current approved plant configuration; forecasts do not unlock entry. The available window grows with the archive. Saved readings remain visible after configuration changes. Import energy in explicit MWh or kWh, with timezone-aware bounds and quality labels. Exact replays are idempotent; conflicting or overlapping records reject the entire batch. PostgreSQL exclusion constraints enforce non-overlap, requiring the `btree_gist` extension. An import contains at most 2,000 rows; larger histories use multiple batches. Observations are immutable in this version; corrections need an explicit replacement-history design before they can be supported safely.

Saving production automatically prepares a durable calibration job once at least seven complete, valid matched days exist within the 90-day window ending on the latest recorded local day. Additional readings can be saved while a job is running; terminal completion prepares one follow-up if new eligible data arrived. An unchanged dataset does not trigger repeated jobs. The manual API retains its 7–90 completed-day range for operator compatibility. A separate Dramatiq `calibration` queue performs offline fitting without provider calls; leases expire after five minutes and permit at most three attempts. Pin observation creation time and ID watermark, source snapshots, configuration, selection policy and a dataset digest. Recheck configuration and lease ownership before publication. Activation and baseline rollback are explicit tenant operations, recorded separately from the immutable fit. Details and statistical acceptance criteria live in `IRRADIANCE.md`.

Electrical forecasts select the first eligible forecast acquired on the preceding local day. An explicit `as_of` excludes acquisitions and activations not yet available then. Calibration instead pairs metered production with the latest matching estimated actuals acquired and published by the calibration request cutoff; it never falls back to forecasts. Preserve immutable acquisitions, source kinds and publication times. Later revisions cannot change a completed fit or an archived forecast. Missing history yields insufficient calibration data.

The plant screen opens tomorrow in the region's timezone. Its automatic source prefers estimated actuals for completed past days, falling back to the archived forecast when history is absent. Explicit source selection preserves access to both series; the API defaults to forecasts for compatibility. A missing configuration or source returns `freshness: missing`, a null result and `unavailable_reason`. Historical electrical estimates use the declared configuration with factor 1 and label retrospective configuration assumptions; they are not metered output. Configuration revisions are never backdated to fill a gap.

`collection-schedule/` exposes region times and the shared account's remaining UTC budget. One daily request is reserved in the per-region schedule limit for history; this is a planning limit, not a guaranteed allocation across regions. Snapshot coverage indexes support history lookup, and `available_at` records atomic publication separately from acquisition. Empty history remains missing and the next daily retrieval can recover it. A schedule change cancels unclaimed work; an authorized in-flight response can still be archived if its binding remains current.

Implement in this order:

1. Compatibility checks, locked environment, PostgreSQL, authentication and tenant isolation.
2. Region request and admin approval flow using a fixture provider.
3. Durable jobs, account quota, live provider adapter and immutable snapshots.
4. The calculation contract in `IRRADIANCE.md`, results API and reference evaluation.
5. Restart/concurrency/security tests, Windows/Ubuntu CI and reproducible startup instructions.
6. Plant configuration, clipped interval generation, metered-production import, offline calibration, explicit activation/rollback and forecast provenance.

Acceptance: a second tenant cannot access the first tenant's records; concurrent approvals/jobs preserve invariants; retries respect quota; restart/redelivery does not duplicate results; invalid secrets remain private; incomplete days are not reported as zero; both OS environments pass the implemented flows. Without live credentials or matched labels, finish the fixture-backed system and identify the unverified integration/accuracy boundary.

## Saved-forecast email reminders

Each member can configure daily local `HH:MM` times for an approved region, with one optional recipient override for that region. Otherwise use the member's default account email. The current provider plan's daily request limit also caps daily reminder times; this is a product limit, not a quota reservation. Emails make **zero provider requests** and do not consume the request ledger. A reduced plan limit retains only the earliest configured times within the current limit.

The dispatcher pins the latest current-binding forecast acquired and published at or before the scheduled time, along with the recipient. Never substitute a later acquisition after a delayed dispatch. Email contains today's and tomorrow's irradiance summaries, completeness, units, source age and an interval CSV; incident energy is distinct from plant electricity. An age over 24 hours is labeled stale, not silently corrected. Missing saved data is an explicit unavailable outcome.

Use region IANA timezones, skip nonexistent DST times and use the first occurrence of repeated times. Catch up at most two hours; older missed slots are recorded without sending a backlog. Unique database slots and a separate notification queue prevent concurrent duplicate sends. Recheck membership, approval, preferences, the current recipient and binding revisions before sending. A changed recipient cancels pending delivery rather than sending to the old address. A known non-send may retry three times; ambiguous SMTP acceptance or interrupted sending becomes `uncertain` without automatic retry. SMTP acceptance does not prove inbox delivery. Provider/mail errors never expose credentials.

## Primary references

- [Python 3.15.2](https://www.python.org/downloads/release/python-3152/), [Django 6.1.3](https://docs.djangoproject.com/en/6.1/releases/6.1.3/), [DRF releases](https://www.django-rest-framework.org/community/release-notes/).
- [pvlib](https://pvlib-python.readthedocs.io/en/stable/), [Dramatiq releases](https://pypi.org/project/dramatiq/), [Dramatiq platform history](https://dramatiq.io/changelog.html), [Celery Windows limitation](https://docs.celeryq.dev/en/stable/faq.html#does-celery-support-windows).
