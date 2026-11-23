# Viewer proposes a production or outage change and follows its outcome

A workspace viewer cannot write production or outage data directly. Instead the viewer posts an idempotent change request (corrected production intervals of one day, or an outage create, update, resolve or cancel) with a reason. It stays `pending` and changes no calculation input until an administrator decides (`change-request-review`). The viewer sees pending requests per plant, can withdraw one, and marks an approved or rejected outcome as seen, one by one or all at once, to clear the unread counter.

## Entry points

- `web/src/features/production/production-history.tsx` `ProductionHistory` (role from `useCanManageWorkspace`; each row says through `can_propose`, `can_propose_revert` and `proposal_unavailable_reason` whether it can be proposed, and a member sees one notice when nothing on the day can): the form `web/src/features/production/history/interval-editor.tsx` `IntervalEditor` stages one interval at a time into the draft `web/src/features/production/history/proposal-draft.tsx` (model `web/src/features/production/history/proposal.ts`), which sends the day's staged intervals as one request with one reason. `web/src/features/plant/outages.tsx` `Outages` with `web/src/features/plant/outage/model.ts` `submitOutageChange` (create, update, resolve, cancel).
- Own pending marker: each production interval and each outage of the list carries `pending_request_id`, the actor's own latest pending request on it (`production-history`, `outage-management`), shown as "Waiting for approval – view" (`web/src/features/inbox/pending-approval.tsx`) linking to `inbox/?request=<id>`, which opens that request's detail above the inbox list.
- `web/src/lib/idempotency.ts` `useProposalKey`: one `crypto.randomUUID()` per payload JSON. After a submit the callers refetch the workspace queries (`useRefresh().workspace`), which includes the tenant counters. `web/src/features/inbox/operational-inbox.tsx` `OwnPendingRequests` (per-plant notice on the Production and Outages tabs, polled every 120 s), `RequestDetail` (Withdraw, Mark as read) and the Mark all read button (viewers only get scope `mine`).
- `POST /api/tenants/{t}/regions/{r}/plants/{p}/change-requests/production/` -> `pyracast.change_requests.api.ProductionRequestsView`; `.../incidents/` -> `IncidentRequestsView` (routed in `pyracast.plants.urls`). Operations `submit_production`, `submit_incident`. `POST /api/tenants/{t}/change-requests/{q}/withdraw/` -> `WithdrawView`; `.../{q}/seen/` -> `SeenView`; `.../change-requests/seen/` -> `SeenAllView` (`pyracast.change_requests.urls`; `withdraw_request`, `mark_seen`, `mark_all_seen`). All are member routes of the route policy. Actor: viewer. No command, job, email or external call.

## Sequence

```mermaid
sequenceDiagram
    actor V as Viewer
    participant UI as ProductionHistory or Outages
    participant API as Request views
    participant Op as change_requests.operations
    participant DB as PostgreSQL
    V->>UI: "Stage intervals of the day or enter an outage, give a reason, send"
    UI->>API: "POST change-requests/production/ or incidents/ with CSRF token and idempotency_key"
    alt no session, not a member or invalid body
        API-->>UI: "401, 403 or 400"
    end
    API->>Op: "submit_production or submit_incident"
    Op->>DB: "BEGIN, lock tenant, read membership, replay lookup by key"
    opt no stored request with this key
        Op->>DB: "lock region and plant, refuse a new outage by outage_proposal_refusal, check heads or incident, count pending, insert request and items, set baselines"
    end
    alt administrator, reused key, invalid values, stale revision, limit or lock timeout
        Op-->>API: "exception, transaction rolled back"
        API-->>UI: "403, 400, 404, 409 or 503"
    end
    Op-->>API: "new request, or the stored one with its current status"
    API->>DB: "summary built in the same transaction, then COMMIT"
    API-->>UI: "201 ChangeRequestSummaryOutput, draft or editor closes, queries refetch"
    UI->>API: "POST change-requests/{q}/withdraw/, seen/ or change-requests/seen/ from the inbox"
    API->>Op: "withdraw_request, mark_seen or mark_all_seen"
    Op->>DB: "lock tenant then request row, check authorship, set withdrawn or outcome_seen_at once"
    alt not own, or withdraw after a decision
        API-->>UI: "404 not_found or 409 request_already_closed"
    else
        API-->>UI: "200 summary (seen all: marked count)"
    end
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `change_requests_changerequest` | R/W | Insert `pending` row with `proposer_membership_id`, `idempotency_key`, `payload_sha256`; withdraw and seen update it; replay, limit count, visibility and counters read it |
| `change_requests_productionchangeitem` | R/W | One row per interval (normalized MWh, pinned `expected_head`), then baseline columns updated; `baseline_source_kind` is `manual`, `realtime_generation`, `settlement_injection` or empty |
| `change_requests_incidentchange` | R/W | One row: action, target incident, `expected_revision`, proposed values or `ends_at`; read back for the response and its pending validation |
| `accounts_tenant`, `regions_region`, `plants_plant` | R (`FOR UPDATE`) | Locked in that order by `tenant_membership(lock=True)`, `lock_production_configuration` or `lock_incident_plant` |
| `accounts_membership` | R | Role, active user; its pk is the proposer identity |
| `plants_plantconfiguration`, `connections_sitebinding`, `connections_provideraccount` | R | Production only: configuration must be current (plant current, region approved, binding approved for the region revision, provider account enabled and authorized). Outage create pins the plant's current configuration; update pins it unless the proposed values end the outage, then the incident's declared one; resolve and cancel keep the declared one; no binding or provider check |
| `plants_productionobservation`, `metering_epiasconnection`, `metering_epiassnapshot`, `incidents_plantincident`, `incidents_incidentrevision` | R | Heads, provider and effective values for baseline and response; incident target locked and overlap-checked |

## Authorization

- Authentication: `SessionAuthentication` with CSRF plus `RoutePolicy` (`REST_FRAMEWORK` in `settings.py`). Route: `MEMBER` admits any active member of `tenant_id`, administrators included; production proposals are throttled under `production_import` (120 an hour per user), outage proposals under `submission` (30 an hour).
- Operation: `tenant_membership(role=viewer, lock=True)` locks the tenant and rejects every other role with `admin_edits_directly`; it runs after serializer validation, so it is the only block on administrators. Withdraw, seen and seen-all use `tenant_membership(lock=True)` (any role), `visible_requests`, then both `proposed_by_id` and `proposer_membership_id` must match, else 404 (also for administrators).
- Database: unique `changerequest_idempotency_key_uniq` on (`proposer_membership_id`, `idempotency_key`), `changerequest_review_chk`, `changerequest_decision_reason_chk`, `productionchangeitem_*` and `incidentchange_*` checks. `proposer_membership_id` is a plain integer, not a foreign key.

## Failure paths

| Condition | Result |
| --- | --- |
| No session | 401 `not_authenticated` with `WWW-Authenticate: Session` |
| Wrong CSRF token | 403 `csrf_failed` |
| Non-member | 403 `membership_required` |
| Workspace administrator submits a valid body | 403 `admin_edits_directly`, "Workspace members submit requests; administrators edit directly." |
| Unknown field, bad type, blank or over-500 reason (trimmed), no interval or over 100 intervals | 400 `invalid_request` with field errors |
| Operation rules (interval not completed or aligned, overlap, over 31 local days, production configuration or binding not current, revert without manual value, incident fields) | 400 `invalid_request`, or the incident code (`incident_overlap`, `incident_starts_in_future`, `incident_closed`, ...) |
| New outage on a plant without a current configuration, or at the pending limit (decided by `outage_proposal_refusal` after the request shape is validated and a replay is answered, before the outage's own rules are checked) | 400 `configuration_unavailable`, or 409 `pending_request_limit` |
| Region, plant, configuration or incident missing, trashed or elsewhere; request not own or its location trashed | 404 `not_found` |
| Same key with another payload / 20 pending on this plant / withdraw after a decision | 409 `idempotency_key_reused` / `pending_request_limit` / `request_already_closed` |
| Stale incident `expected_revision` | 409 `incident_revision_changed` with the `current` incident |
| Stale production `expected_revision_id` | 409 `production_revision_changed`; no request row is stored |
| Throttle used up | 429 `throttled` |
| Lock wait over 5 s / same session still running | 503 `busy`, `Retry-After: 1` / 429 `session_busy` |

## Rules and invariants

- Lock order Tenant, Region, Plant; withdraw, seen and seen-all lock Tenant then the request row. The replay lookup precedes the plant lock, the pending count follows it. Idempotency scope is membership plus key across plants. The production digest hashes plant, region, configuration, unit, changes and stripped reason; the incident digest hashes plant, region, action, incident, expected revision, values, `ends_at` and reason. Any stored request, even a terminal one, replays as 201 with its current status. The summary is built inside the action's transaction, so a replay after the location was trashed returns the stored summary. The browser keeps one key per payload JSON and resets it on success, on production cancel and when an outage create, update or resolve form opens.
- Production requests take 1 to 100 intervals (direct writes 2,000; the screen sends the staged intervals of one day), follow the direct-write interval and head-revision rules, store energy as MWh and pin each target head. At most 20 pending per membership and plant (`PENDING_LIMIT` in `change_requests.policy`). Production reason required (1 to 500), outage reason optional.
- The draft locks the day navigation while it exists (`production-history`). A refusal keeps it: an item is named from its `changes.<n>` fields, or as stale (its revision changed or it can no longer be staged) once the day is read again; a refused `reason` is shown at its field and other field errors that name no item in the draft's notice. A reason of only spaces is refused at its field before sending.
- The server accepts a second request of a member for an interval or outage that already holds their own pending request (the marker names the latest), and requests of different members for one interval. The screen does not: such an interval or outage offers only "Waiting for approval – view" (no "Suggest correction", no outage change, resolution or cancellation), and a draft cannot stage that interval (`proposal.ts` `canStage`).
- Authority follows the membership row id: a removed and re-invited viewer loses old requests; an administrator transfer keeps the id, so a promoted author can still withdraw.
- `unread` means own, approved or rejected, `outcome_seen_at` empty; reads never mark it, but opening one's own unread decided request in the inbox posts `seen` once (a failed post keeps the detail and its Mark as read button), `seen` on a pending or withdrawn request is a 200 no-op, seen-all marks only the actor's own unread requests, and a withdrawn request cannot carry `outcome_seen_at` (check constraint).

## Tests

- `tests/test_change_request_boundaries.py` (API submit and withdraw, seen receipt, replay, removed proposer, tenant isolation), `tests/test_change_request_policy.py` (summary answers, replay after trash, a day of intervals inside the quota, visibility and unread rules), `tests/test_change_request_concurrency.py` (shared key, last pending slot, membership removed while waiting), `tests/test_change_request_contracts.py` (reason and interval limits, incident fields, incident replay, administrator cannot withdraw or mark another's request, withdraw retry), `tests/test_change_requests.py` (invalid numbers, stale production revision, pending requests absent from measurements, capability flags, trash and purge, calculation isolation), `tests/test_mark_all_read.py`, `tests/test_change_request_constraints.py`, `tests/test_own_pending_markers.py` (markers, a refused item of a multi-interval request), `tests/test_outage_proposal_clarity.py` (the list's offer equals what submission accepts, limit before validation, a second pending request per interval or outage).
- `web/tests/change-requests.spec.ts`: viewer production proposals with 503 retry and stable key at 320 px, outage proposal envelopes, why an interval cannot be proposed, withdraw and mark-read (one and all) in Turkish; every API call is mocked with `page.route`.
- Gaps: no HTTP test of the administrator refusal, `idempotency_key_reused` or `request_already_closed` on these routes (the code mappings are pinned in `tests/test_error_contract.py`), or of a stale incident revision on the proposal route; no Playwright run against the real backend.
