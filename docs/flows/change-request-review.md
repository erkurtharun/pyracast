# Administrator reviews the change-request inbox

The workspace administrator opens the Inbox, lists pending, decided or own change requests and opens one to compare the member's proposal with its review context: for production the current effective and provider values of each interval, for an outage the incident revision the proposal was based on. Approval revalidates the request under locks, requires the review fingerprint returned with the detail and applies the change through the same production or incident operation used for direct edits; rejection requires a reason. Members see only their own requests on the same screen (withdraw and mark-seen belong to `change-request-submission`).

## Entry points

- `web/src/features/workspace/workspace-shell.tsx` shows the Inbox nav item to every member with a badge of `pending_review_count` (admin only) plus `unread_outcome_count` from `GET /api/tenants/` (`pyracast.api.views.TenantsView`, polled every 120 s; see `workspace-switching`). the Inbox route (`workspace-pages.tsx`) renders `web/src/features/inbox/operational-inbox.tsx` `OperationalInbox` at `/app/w/<tenant>/inbox/`, keyed by tenant id and role. After a decision `OperationalInbox` refetches the workspace queries (`useRefresh().workspace`), counters included.
- `OperationalInbox` -> its list component `InboxPage` -> `RequestDetail` (with `ProductionProposal`, `IncidentProposal`). Admins pick scope `pending`, `history` or `mine`; members are forced to `mine`.
- `GET /api/tenants/{t}/change-requests/?scope=&page=` -> `pyracast.change_requests.api.InboxView` (`queries.inbox`, `queries.request_summaries`), polled every 30 s. The optional `plant_id` filter is sent only by `OwnPendingRequests` (`change-request-submission`).
- `GET /api/tenants/{t}/change-requests/{q}/` -> `DetailView` (`queries.request_detail`), fetched when Details opens and on "Refresh details"; not polled.
- `POST .../{q}/approve/ {review_fingerprint}` -> `ApproveView` (`operations.approve_request`). Actor: workspace administrator.
- `POST .../{q}/reject/ {reason}` -> `RejectView` (`operations.reject_request`). Actor: workspace administrator.
- No command, script or Dramatiq job. Approval records that the plant needs calibration preparation (`calibration_preparation_due_at`, `plants.preparation.request_preparation`): one attempt runs after commit and the dispatcher's `calibration` phase (`pyracast.plants.runtime.dispatch_calibrations`) prepares what is left, which can create a pending `CalibrationRun` for the `calibrate_plant` actor.

## Sequence

```mermaid
sequenceDiagram
    actor A as Administrator
    participant UI as OperationalInbox
    participant V as ChangeRequest views
    participant Q as queries
    participant Op as approve_request
    participant App as Production or incident operation
    participant DB as PostgreSQL
    UI->>V: "GET change-requests/?scope=pending&page=n"
    V->>Q: "inbox + request_summaries"
    Q->>DB: "visible_requests for the actor's active membership"
    V-->>UI: "200 page of summaries, 50 per page"
    UI->>V: "GET change-requests/q/ when Details opens"
    V->>Q: "request_detail, no row locks"
    Q->>DB: "read request, items, observation heads, provider snapshots, incidents"
    Q->>Q: "validate_request with lock=False, compute review_fingerprint"
    V-->>UI: "200 detail with review_fingerprint and conflict or null"
    UI-->>A: "proposal and context, a conflict shows a notice and disables Approve"
    A->>UI: "Approve"
    UI->>V: "POST q/approve/ with review_fingerprint and X-CSRFToken"
    V->>V: "session auth, CSRF, route policy admin, ApprovalInput"
    alt viewer or unknown field
        V-->>UI: "403 or 400"
    end
    V->>Op: "actor, tenant, request, fingerprint"
    Op->>DB: "BEGIN, check admin and lock Tenant, read request, lock Region and Plant, lock ChangeRequest"
    alt status already approved
        Op-->>V: "return row unchanged"
    else status rejected or withdrawn
        Op-->>V: "409 request_already_closed"
    end
    Op->>DB: "validate_request with locks, an incident proposal locks its target PlantIncident"
    alt revision, configuration or overlap conflict
        Op-->>V: "409 request_conflict with localized message"
    end
    Op->>Op: "recompute fingerprint from decision_context"
    alt fingerprint differs
        Op-->>V: "409 review_context_changed"
    end
    Op->>DB: "store reviewed_context"
    Op->>App: "apply as the administrator"
    App->>DB: "observation heads and PlantEvent, or incident revision; request calibration preparation"
    Op->>DB: "link applied ids, status approved, reviewed_by, closed_at"
    V->>DB: "summary built in the same transaction, then COMMIT"
    V-->>UI: "200 summary, the UI then refetches detail, list and tenant counters"
    opt any approve error
        UI-->>A: "error plus contextChanged notice, Approve disabled until Refresh details loads a new detail"
    end
```

Rejection takes the Tenant, Region, Plant and ChangeRequest locks and the same status checks, stores `reviewed_context`, then closes with `decision_reason`; it does not validate the proposal or the fingerprint.

## Data

| Table | R/W | What |
| --- | --- | --- |
| `accounts_tenant` | R (lock on decisions) | Row lock serializes the decision with administrator transfers |
| `accounts_membership`, `auth_user` | R | Actor role and `is_active`; proposer and reviewer names; whether the proposer is still a member (`former_member`) |
| `regions_region`, `plants_plant` | R (lock on decisions) | Scope, trash state (`regions_region.deleted_at`), `current_configuration`; `plants_plant.calibration_preparation_due_at` is set by an approval |
| `plants_plantconfiguration`, `connections_sitebinding`, `connections_provideraccount` | R | Production: pinned configuration must still be current (the plant's current one, binding approved for the region revision, provider account enabled and authorized); binding timezone. Outage create or update: pinned id must equal the plant's current configuration, or the declared one of an ended target; resolve and cancel check no configuration, binding or provider |
| `change_requests_changerequest` | R/W | Status, `reviewed_by`, `closed_at`, `decision_reason`, `outcome_seen_at`, `reviewed_context` |
| `change_requests_productionchangeitem` | R/W | Proposed intervals; `applied_observation` set on approval |
| `change_requests_incidentchange` | R/W | Proposed incident action and values; `applied_revision` set on approval |
| `plants_productionobservation` | R/W | Current heads for context; approval supersedes heads and inserts new revisions |
| `metering_epiasconnection`, `metering_epiassnapshot`, `metering_epiascollectionjob` | R | Snapshots of the enabled connection give `automatic` and `provider_intervals` |
| `incidents_plantincident`, `incidents_incidentrevision` | R/W | Target, baseline revision (`before`) and overlapping incidents; outage overlay on production values; approval saves the incident and a revision |
| `plants_plantevent` | W | `production_changed` (origin `change_request`) when a production approval writes a value |
| `plants_calibrationrun` | R/W | Open runs whose outage inputs changed are superseded by an incident approval; new runs come from preparation after commit (see `calibration-automation`) |

## Authorization

- Session authentication for every endpoint; DRF enforces CSRF on the POSTs of a signed-in session. Anonymous callers get 401.
- Route policy (`pyracast.api.permissions.ROUTE_POLICIES`): approve and reject are `ADMIN` routes (active administrator membership, checked before the body is validated); the inbox and detail GETs are `MEMBER` routes.
- Operations re-check: `pyracast.accounts.authorization.tenant_membership(role=admin, lock=True)` reads the membership, then locks the tenant and reads it again (again inside `change_production`, `save_incident`, `finish_incident`); reads use `tenant_membership` without a lock.
- Row scope in `change_requests.policy.visible_requests`: the request's location must not be trashed and the request belongs to the tenant; admins see every request, others only rows where both `proposed_by` and `proposer_membership_id` match the current membership, so a rejoined user does not regain old requests. `inbox`, `request_detail` and `request_summaries` use the same policy. Only a reviewer receives `review_fingerprint`. Platform staff status grants nothing here.

## Failure paths

| Condition | Result |
| --- | --- |
| Not signed in | 401 `not_authenticated` with `WWW-Authenticate: Session` |
| POST without a valid CSRF token | 403 `csrf_failed` |
| Not a member of the tenant (GET or POST), platform staff included | 403 `membership_required` |
| Viewer posts approve or reject | 403 `workspace_admin_required`, "Workspace administrator required." |
| Request in another tenant, another member's request, location in the trash | 404 `not_found` |
| `page` past the last page or not a number | 404 `not_found`; the pager stays, so Previous works |
| Unknown query or body field, bad `scope`, `plant_id` below 1, missing or over-long `review_fingerprint`, blank or over-long reject reason | 400 `invalid_request` |
| Same decision repeated | 200 with the existing outcome; the fingerprint is not rechecked and a new rejection reason is ignored |
| Opposite decision or withdrawn request | 409 `request_already_closed` |
| Production head or incident revision changed, configuration no longer current, overlap, target incident cancelled, resolve of an already ended incident (update and cancel of an ended incident are allowed) | 409 `request_conflict` with the localized reason; request stays pending |
| Review values or heads changed since the detail was read | 409 `review_context_changed` |
| Lock wait over `lock_timeout` 5 s / same session still running | 503 `busy`, `Retry-After: 1` / 429 `session_busy` |
| Pending request currently invalid (detail read) | 200 with `conflict.code = request_conflict`; Approve disabled |

## Rules and invariants

- Lock order for decisions: Tenant, Region, Plant, ChangeRequest; approving an update, resolve or cancel proposal then locks the target `PlantIncident`. Inner operations re-acquire the same locks re-entrantly. A failed approval rolls back everything, including the stored `reviewed_context`.
- Fingerprint is `"v1:" + sha256` of canonical JSON (`preparation.digest`) over `review_context`: request id, payload hash, plant's current configuration id, and either per-interval bounds, `current`, `automatic`, `provider_intervals`, `warnings` plus the ids of overlapping current observation heads, or `(id, revision)` of the target incident and, for create and update, of every overlapping non-cancelled incident. Provider snapshot ids are excluded, so an unchanged reacquisition keeps the fingerprint.
- Decisions are idempotent per outcome. Status moves only from `pending` to `approved` or `rejected` (here) or `withdrawn` (proposer); `changerequest_review_chk` pins which fields each status may set and `changerequest_decision_reason_chk` refuses a rejection without a reason.
- `reviewed_context` (version 1) freezes the decision-time view; approved and rejected production details are rendered from it, not from live data (pending and withdrawn ones are computed live).
- Approval runs as the administrator: new observations and incident revisions record the admin, while the request keeps the proposer. Production is applied with origin `change_request` and the approval counts as the plausibility confirmation. Unchanged production values reuse the existing head as `applied_observation`. When the deciding admin is also the proposer, `outcome_seen_at` is set at close, so no unread badge appears.
- Visibility, pending, unread, authorship and former-member rules live in `change_requests.policy`, shared with the workspace counters. Decision answers are the request summary, built in the decision's transaction.
- The detail never takes row locks; its conflict check can be stale by the time Approve is pressed, which the locked revalidation and fingerprint check catch. Submission times show as device times with their offset; request kinds, statuses, qualities and outage kinds come from the vocabulary; a failed decision shows a localized message.

## Tests

- `tests/test_change_requests.py`: review values and frozen approval, revert proposals, half-hour provider evidence, fingerprint stability, derived conflicts, rollback on failed approval, inbox filter validation, ownership recheck.
- `tests/test_change_request_boundaries.py`: role and tenant isolation, opposite decisions, rejoined proposer, admin transfer, deactivated user, tenant badge counters, cross-tenant ids for every endpoint. `tests/test_change_request_policy.py`: summary answers, only a reviewer receives the fingerprint, visibility and unread rules. `tests/test_change_request_constraints.py`: rejection reason.
- `tests/test_change_request_concurrency.py`: real PostgreSQL threads for concurrent approvals, lock winners, direct writes versus waiting approval, member removal and admin transfer versus waiting approval, provider publication versus waiting review and versus an unlocked detail read.
- `tests/test_change_request_contracts.py`: localized conflict detail, approve and reject over HTTP with a repeated rejection and the `history` and `plant_id` filters, compiled Turkish text, frozen rejected history, period and outage in summaries, query counts. `tests/test_mark_all_read.py`: seen-all.
- Playwright `web/tests/change-requests.spec.ts` (mocked API): mobile review where a 409 approve blocks until Refresh details, paging and required rejection reason, role changes clearing controls, last-page navigation including the 404 page, review cards with period, count and outage title.
- Gaps: no Playwright test against the real backend (`web/integration` does not cover the inbox); the 503 `busy` mapping is pinned only generically in `tests/test_api_contention.py`, not through these views; no UI test distinguishing a 503 `busy` approve error from a stale context; no test for the `ObjectDoesNotExist` branch of the detail conflict text; no backend test posts a blank or over-long rejection reason or a missing fingerprint.
