# Backend plan

## Outcome

Build a multi-tenant backend where customers request regions, a platform administrator links authorized Solcast accounts and approves them, and scheduled jobs publish tomorrow's irradiance and daily energy estimates. The customer frontend is separate; provide its API and a minimal internal admin interface.

Use only provider accounts authorized for this service. Solcast account creation remains an operator task outside this backend. Implement fixtures so development does not depend on credentials or live API calls.

## Stack and portability

- CPython **3.15.2**, Django, Django REST Framework, PostgreSQL and pvlib.
- Dramatiq with RabbitMQ is the initial worker candidate for native Windows/Ubuntu operation. Keep tasks thin so a verified better executor can replace it.
- One repository, with API, worker and scheduler/dispatcher as separate processes. Keep PostgreSQL as the durable source of truth.
- API, numerical core and worker must run on Windows x64 and Ubuntu x64. PostgreSQL and RabbitMQ may be local services, remote services or Docker Compose dependencies. Full-stack containers are an additional deployment option.
- Use portable Python entry points, `pathlib`, UTF-8, timezone-aware dates and an explicit `tzdata` dependency. Correctness must not depend on Bash, cron, Unix signals, `fork`, or development reload/watch features.

At bootstrap, verify and lock the latest stable compatible dependencies and container images. Python 3.15.2 is fixed; this document is **not a tested compatibility matrix**. Verify transitive scientific dependencies, installation, migrations, an API request, a pvlib calculation and a real queued task on both operating systems. If blocked, report the exact incompatibility and smallest alternative; continue independent work.

Celery has no supported native Windows runtime. Selecting Celery would require an explicit Linux-container/WSL worker contract; it must not be presented as native Windows support.

## Boundaries and records

Use conventional Django apps for accounts, regions, provider connections and forecasts; keep the calculation package independent of Django. API views, admin actions and tasks call shared application operations. Avoid business logic in serializers or implicit model signals.

| Record | Owns |
| --- | --- |
| Tenant, User, Membership | Customer ownership and permitted actions |
| Region | Tenant, coordinates, IANA timezone, surface and approval state |
| ProviderAccount | Provider identity, secret reference, entitlement and account-wide quota |
| SiteBinding | Region-to-provider-account/site mapping and immutable configuration revisions |
| CollectionJob, RequestAttempt | Scheduling, leases, quota reservations and execution history |
| ForecastSnapshot, ForecastResult | Raw input, normalized intervals, derived output and model version |
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
- Schedule the next local day's forecast at a configurable local cutoff, initially 16:00. Record its UTC execution slot and local target date. Deduplicate by binding revision, endpoint and scheduled slot; recover missed work within quota and flag late forecasts rather than pretending they met the cutoff.
- Do not hold database transactions open during external HTTP calls. Recheck binding revision and approval before publication. Multiple scheduler instances must not duplicate work.
- Preserve forecast vintages and configuration/model versions. Expose stale or incomplete results explicitly. Monitor last successful collection, queue lag, provider errors, quota, solver failures and coverage; logs carry correlation IDs without credentials.

## API and delivery

Provide authenticated region submission/list/detail, platform-admin connection validation and approve/reject/suspend actions, and dated forecast retrieval. Generate OpenAPI; use explicit units, bounded date ranges, pagination and stable error codes. Approval transitions are operations, not writable status fields.

Implement in this order:

1. Compatibility checks, locked environment, PostgreSQL, authentication and tenant isolation.
2. Region request and admin approval flow using a fixture provider.
3. Durable jobs, account quota, live provider adapter and immutable snapshots.
4. The calculation contract in `IRRADIANCE.md`, results API and reference evaluation.
5. Restart/concurrency/security tests, Windows/Ubuntu CI and reproducible startup instructions.

Acceptance: a second tenant cannot access the first tenant's records; concurrent approvals/jobs preserve invariants; retries respect quota; restart/redelivery does not duplicate results; invalid secrets remain private; incomplete days are not reported as zero; both OS environments pass the implemented flows. Without live credentials or matched labels, finish the fixture-backed system and identify the unverified integration/accuracy boundary.

## Primary references

- [Python 3.15.2](https://www.python.org/downloads/release/python-3152/), [Django 6.1.3](https://docs.djangoproject.com/en/6.1/releases/6.1.3/), [DRF releases](https://www.django-rest-framework.org/community/release-notes/).
- [pvlib](https://pvlib-python.readthedocs.io/en/stable/), [Dramatiq releases](https://pypi.org/project/dramatiq/), [Dramatiq platform history](https://dramatiq.io/changelog.html), [Celery Windows limitation](https://docs.celeryq.dev/en/stable/faq.html#does-celery-support-windows).
