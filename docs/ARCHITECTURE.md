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
| PlantIncident, IncidentRevision | Non-overlapping operating periods, remaining capacities and immutable corrections |
| ProductionObservation | Interval energy, quality, source, configuration and recording time |
| EpiasConnection, EpiasCollectionJob, EpiasSnapshot | Plant identity, independent collection and immutable reported-production acquisitions |
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
- Members configure region-local daily forecast times, initially 16:00. Historical collection runs separately at 03:00, refreshing yesterday and recovering missed days from the available seven-day window. Request `hours=168` explicitly and archive completed intervals; each acquisition remains immutable. Historical transport failures, empty responses and incomplete previous days retry no sooner than 30 minutes later, before 09:00. Restart catch-up is allowed inside that night window; disabling collection stops new night work. Account-wide UTC quota reservations preserve the remaining scheduled forecast needs before admitting a night request. Quota exhaustion or reserved forecast budget never creates a rapid retry loop. Configuration changes apply to future forecast slots; they do not invent missed forecast acquisitions. Use unique slots, advance a nonexistent night start to the first valid half-hour before 09:00, choose the first repeated time and flag late acquisitions.
- Do not hold database transactions open during external HTTP calls. Recheck binding revision and approval before publication. Multiple scheduler instances must not duplicate work.
- Preserve forecast vintages and configuration/model versions. Expose stale or incomplete results explicitly. Monitor last successful collection, queue lag, provider errors, quota, solver failures and coverage; logs carry correlation IDs without credentials.

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
remains independent. Resolve the separate settlement plant ID by EIC and initially collect the
completed calendar months covering that 90-day window (at least three). Reconcile the previous two months daily
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

Serve the frontend and `/api/` through one origin. Django owns authentication and authorization; JSON session endpoints enforce CSRF even before login. Keep sessions in HttpOnly cookies and API responses private and non-cacheable. The frontend reads stored results and never handles provider credentials. Forwarded HTTPS headers are trusted only when explicitly enabled behind a private, trusted proxy. Rate-limit authentication at the deployment ingress.

Keep the customer interface compact and responsive: primary values and actions stay visible; source details and extended guidance use disclosures. Preserve units, missing values, quality warnings and CSV access. Tables scroll within their own focusable region; navigation and forms fit narrow screens. Show the journal's local timezone once, retaining interval offsets when a daylight-saving transition makes local times ambiguous.

Plant operations use the same tenant membership boundary. Configuration revisions pin the approved reference binding and distinguish DC nameplate, inverter AC capacity, grid export limit and the measurement point (`net_export` or `ac_generation`). Unknown capacities remain null. Changing the configuration supersedes pending calibration work; it does not rewrite historical forecasts or observations.
Mounting metadata is also revisioned: unknown, fixed or tracking, with optional fixed-array tilt and clockwise-from-north azimuth. It is not consumed by the current reference-transfer proxy. In particular, a 100 MW grid export limit does not imply a 100 MW inverter rating.

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

The production journal selects one completed local day at 30/60-minute resolution and shows whole meter intervals overlapping astronomical daylight. Astral calculates a sea-level horizon from immutable binding coordinates and timezone without provider requests. Previous/next controls preserve unsaved entries in memory; saved observations, including night and previous configurations, remain in a separate expandable list. The API retains explicit full-day/range access for integrations. New meter entries and CSV imports require complete matching archived estimated-actual coverage and the current approved plant configuration; forecasts do not unlock entry. The available window grows with the archive. Saved readings remain visible after configuration changes. Import energy in explicit MWh or kWh, with timezone-aware bounds and quality labels. Exact replays are idempotent; conflicting or overlapping records reject the entire batch. PostgreSQL exclusion constraints enforce non-overlap, requiring the `btree_gist` extension. An import contains at most 2,000 rows; larger histories use multiple batches. Observations are immutable in this version; corrections need an explicit replacement-history design before they can be supported safely.

Saving production automatically prepares a durable calibration job once at least seven days have complete, valid matched daylight coverage within the 90-day window ending on the latest recorded local day. Additional readings can be saved while a job is running; terminal completion prepares one follow-up if new eligible data arrived. An unchanged dataset does not trigger repeated jobs. The manual API retains its 7–90 completed-day range for operator compatibility. A separate Dramatiq `calibration` queue performs offline fitting without provider calls; leases expire after five minutes and permit at most three attempts. Pin observation creation time and ID watermark, source snapshots, configuration, selection policy and a dataset digest. Recheck configuration and lease ownership before publication. Activation and baseline rollback are explicit tenant operations, recorded separately from the immutable fit. Details and statistical acceptance criteria live in `IRRADIANCE.md`.

The current/future plant outlook uses `latest-forecast-coverage-v2` for the current approved configuration. Each published forecast owns its coverage window in newest-first order; older forecasts fill only time outside newer coverage, so an intraday update does not discard the morning. Nulls, missing rows and zeros inside newer coverage remain authoritative. Record source snapshots, coverage segments, native interval bounds and per-output interval source IDs. Both normal and outage scenarios use the same composition; freshness follows the oldest contributing acquisition. Collection at each configured time refreshes this outlook, including intraday updates; open forecast screens refresh saved results every 30 seconds without provider requests. Historical electrical forecasts and explicit `as_of` replays retain the first eligible forecast acquired on the preceding local day, excluding acquisitions and activations not yet available then. Calibration instead pairs production with the latest matching estimated actuals acquired and published by the calibration request cutoff; it never falls back to forecasts. Preserve immutable acquisitions, source kinds and publication times. Later revisions cannot change a completed fit or an archived forecast. Missing history yields insufficient calibration data.
Calibration jobs pin the incumbent activation under the plant lock. An eligible candidate must satisfy the current numerical policy and cannot replace a different activation selected after fitting was requested. Restore-to-baseline remains explicit. The performance API and frontend compare historical day-ahead input replays to meter readings for the current configuration. The frontend uses daylight scope; the API also retains explicit full-day evaluation, with missing days and publication cutoffs exposed; the statistical contract is in `IRRADIANCE.md`.

The plant screen opens tomorrow in the region's timezone. Its automatic source prefers estimated actuals for completed past days, falling back to the archived forecast when history is absent. Explicit source selection preserves access to both series; the API defaults to forecasts for compatibility. A missing configuration or source returns `freshness: missing`, a null result and `unavailable_reason`. Historical electrical estimates use the declared configuration with factor 1 and label retrospective configuration assumptions; they are not metered output. Configuration revisions are never backdated to fill a gap.

When a plant was first configured after the selected day began, the ordinary outlook can project that day's archived forecast using the current approved configuration and factor 1. Mark `historical_configuration_assumed`, retain the original day-ahead source cutoff and record configuration creation time. This is a temporary projection, not a new archived plant forecast. Explicit `as_of` queries remain strict; existing historical configurations and calibrated forecasts keep their original selection rules.

`collection-schedule/` exposes multiple daily region-local forecast times, the fixed night window, recent collection outcomes and the shared account's tracked UTC budget. Count successful, failed and uncertain/reserved attempts, including validation, retries and operator checks; usage outside this application is unknown. One daily request is reserved in the per-region schedule limit for history; this is a planning limit, not a guaranteed allocation across regions or a promise of unlimited night retries. Snapshot coverage indexes support history lookup, and `available_at` records atomic publication separately from acquisition. Partial history remains readable and retries create new immutable acquisitions. Saving unchanged settings preserves the effective boundary and all jobs. Forecast edits retain night jobs and queued forecast work at clocks still configured, cancel removed forecast clocks, and only generate newly added forecast clocks after the change. Disabling collection cancels unclaimed night and forecast work. An authorized in-flight response can still be archived if its binding remains current.

Acceptance: a second tenant cannot access the first tenant's records; concurrent approvals/jobs preserve invariants; retries respect quota; restart/redelivery does not duplicate results; invalid secrets remain private; incomplete days are not reported as zero; both OS environments pass the implemented flows. Without live credentials or matched labels, finish the fixture-backed system and identify the unverified integration/accuracy boundary.

## Saved-forecast email reminders

Each member can configure daily local `HH:MM` times for an approved region, with one optional recipient override for that region. Otherwise use the member's default account email. The current provider plan's daily request limit also caps daily reminder times; this is a product limit, not a quota reservation. Emails make **zero provider requests** and do not consume the request ledger. A reduced plan limit retains only the earliest configured times within the current limit.

The dispatcher pins the latest current-binding forecast acquired and published at or before the scheduled time, along with the recipient. Never substitute a later acquisition after a delayed dispatch. Email contains today's and tomorrow's irradiance summaries, completeness, units, source age and an interval CSV; incident energy is distinct from plant electricity. An age over 24 hours is labeled stale, not silently corrected. Missing saved data is an explicit unavailable outcome.

Use region IANA timezones, skip nonexistent DST times and use the first occurrence of repeated times. Catch up at most two hours; older missed slots are recorded without sending a backlog. Unique database slots and a separate notification queue prevent concurrent duplicate sends. Recheck membership, approval, preferences, the current recipient and binding revisions before sending. A changed recipient cancels pending delivery rather than sending to the old address. A known non-send may retry three times; ambiguous SMTP acceptance or interrupted sending becomes `uncertain` without automatic retry. SMTP acceptance does not prove inbox delivery. Provider/mail errors never expose credentials.

## Primary references

- [Python 3.15.2](https://www.python.org/downloads/release/python-3152/), [Django 6.1.3](https://docs.djangoproject.com/en/6.1/releases/6.1.3/), [DRF releases](https://www.django-rest-framework.org/community/release-notes/).
- [pvlib](https://pvlib-python.readthedocs.io/en/stable/), [Dramatiq releases](https://pypi.org/project/dramatiq/), [Dramatiq platform history](https://dramatiq.io/changelog.html), [Celery Windows limitation](https://docs.celeryq.dev/en/stable/faq.html#does-celery-support-windows).
