# Frontend shared layer: HTTP client, queries, polling, forms and UI primitives

Every customer screen reads and writes through one typed client over one request lane. TanStack Query owns server state: `useApiQuery` and `useApiList` load saved data and, when the screen passes `poll`, re-read it without calling providers while the tab is visible. `ActionForm` and `AsyncButton` turn a rejected action into field-level or general messages. The outcome is a screen showing saved data, a localized message, or a per-field error.

## Entry points

- `web/src/lib/api/client.ts` `api` (`get`, `post`, `put`, `patch`, `delete`): paths, parameters, query and body typed from the generated `web/src/lib/schema.d.ts`.
- `web/src/lib/api/lane.ts` `send`, `prepared`, `exchange`: the only `fetch`. CSRF token from the session store, `X-Session-Tag`, `/api/session/` and `/api/auth/` calls serialized, one retry for `session_busy`, 95 s timeout.
- `web/src/lib/api/errors.ts` `errorFromResponse`, `ApiError`, `DisplayError`, `ERROR_CODES`: one error value for every failure.
- `web/src/lib/auth-api.ts` `authRequest` for `/api/auth/browser/v1/*` (allauth headless), through the same lane.
- `web/src/lib/api/query.ts` `useApiQuery`, `useApiList`, `useRefresh`, `cadence`; keys in `web/src/lib/api/keys.ts`; `web/src/lib/session-client.ts` (session state, tag, `loadSession`).
- `web/src/ui/`: `form.tsx` (`ActionForm`, `AsyncButton`, field components), `resource.tsx` `Resource`, `ui.tsx` (`Notice`, `RefreshFailed`, `Empty`), `action.tsx`, `live-region.tsx`; `web/src/lib/idempotency.ts` `useProposalKey`.
- Styling: `web/src/styles/tokens.css` and `tokens.ts`, `web/src/app/globals.css`, enforced by `web/eslint/design-tokens.mjs`.
- Server side: `pyracast.api.errors.exception_handler`, `pyracast.api.session.SessionView`, `pyracast.accounts.session_lock.SessionRequestSerialization`, `pyracast.accounts.session_tag.SessionTagCheck`, `pyracast.accounts.middleware.SessionLifetime`.

## Sequence

```mermaid
sequenceDiagram
    participant S as Screen
    participant H as Query hook or form
    participant L as Request lane
    participant D as Django
    participant DB as PostgreSQL
    S->>H: "useApiList(path, options, poll 30 s)"
    H->>L: "GET url"
    L->>D: "fetch same-origin, no-store, cookies, session tag"
    D->>DB: "session lock, session and usersession write, scoped query"
    alt "ok"
        D-->>L: "200 page with next"
        loop "next is same-origin /api/ and new"
            H->>L: "GET path of next"
        end
        H-->>S: "merged rows, repeated every poll while the tab is visible"
    else "error"
        D-->>L: "status plus code, message, fields"
        L-->>H: "ApiError with message and fields"
        H-->>S: "error, earlier data kept with a refresh notice"
    end
    S->>H: "ActionForm submit"
    H->>L: "onSubmit: POST, PUT, PATCH or DELETE with X-CSRFToken"
    alt "400, 403, 409, 503 busy, 429 session_busy"
        L-->>H: "ApiError"
        H-->>S: "focused alert: field list, else message"
    else "2xx"
        H-->>S: "saved, screen invalidates the workspace queries"
    end
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `django_session` | R/W | `SessionLifetime` refreshes `last_activity_at` at most once a minute on any authenticated request, polls included |
| `usersessions_usersession` | W | The same refresh updates `last_seen_at`, IP and user agent of the device record |
| Advisory lock | W | One per well-formed session cookie serializes that session's requests (5 s wait) |
| Tenant, region and plant tables | R | Whatever the polled URL selects, saved rows only; forecast composition reads one read-only snapshot and takes no row lock; the plant poll's initial scope checks precede that snapshot |

## Authorization

- Browser: `ActionForm` hides submit and disables the fieldset unless `permitted ?? useCanManageWorkspace()`; `AsyncButton` renders nothing under the same rule. Presentation only.
- Transport and view: `SessionAuthentication` (CSRF on unsafe methods, refused as 403 `csrf_failed`; token from `GET /api/session/`), `RoutePolicy` (the route-and-method table in `pyracast.api.permissions`: public, account, member or admin access; a route or method missing from it is refused) and `SessionTagCheck` (409 `session_changed` for a request prepared for another session). Reads are scoped by view querysets, see `docs/flows/tenant-authorization.md`. `CustomerJSONRenderer` redacts provider detail; `PrivateAPIResponses` sets `private, no-store` on `/api/`.

## Failure paths

| Condition | Result |
| --- | --- |
| Non-2xx with a body that lacks `code` and `message` | `Errors.request` with the status. A 2xx non-JSON body throws `Errors.invalidResponse` |
| Contract body `{code, message, fields?, retry_after?, request_id?}` | One `ApiError` (status, code, fields, `retryAfter`, `requestId`, body); the server's localized message is shown as sent, followed by `Errors.reference` when it carries a `request_id` |
| 400 `invalid_request` with `fields` | `fields` become per-control errors in `ActionForm`; the general message stays beside them |
| 429 `session_busy` | The lane repeats the request once after `Retry-After` (at most 2 s), for every method; a second refusal is reported. `busy` (503) is never repeated |
| 409 `session_changed` | Repeated once only when a session read shows the same session under a renewed key; otherwise the caller gets a `session_changed` error |
| 401 `not_authenticated` or 410 `session_ended` on a non-session call | The lane reads the session once, then throws the error; the screen shows the sign-in |
| Connection failure or the 95 s timeout | `Errors.network` for a read, `Errors.unconfirmed` for a change (the change may have been saved) |
| `next` repeats or leaves `/api/` or the origin | `Errors.invalidPage` |
| Read answers 403 or 404 | The hook drops its data, so rows of a workspace the user lost never stay on screen |
| Poll or refresh fails otherwise | Earlier data stays beside a `RefreshFailed` notice with its time and a retry |
| `authRequest` non-2xx | 401 and 404 on `GET account/authenticators/totp` are returned (allauth flow state); others throw through `errorFromResponse`, which reads allauth's `errors[].message` and `param` as message and fields |

## Rules and invariants

- `fetch` appears only in `lane.ts`, always `same-origin` and `no-store`. A `next` link is reduced to path plus query, so the host of a DRF absolute link is never followed.
- Session calls are serialized and a concurrent `GET /api/session/` is shared, so Strict Mode, focus and visibility events make one request.
- Query keys are `["session", "<user id>:<session tag>", workspace or account, path, parameters, query, language]`. Two accounts, two sessions, two workspaces or two languages never share an entry; entries of another owner are removed when the session changes.
- Polls run only in a visible tab and refetch when it becomes visible; a read never overlaps itself. Periods: `cadence.live` 30 s, `steady` 60 s, `background` 120 s. A list read over n pages polls every n periods. Queries do not retry on their own.
- Polls count as activity (`docs/ARCHITECTURE.md`), so a visible tab never hits the 30 minute idle limit, only the 12 hour one.
- A change refreshes through `useRefresh().workspace(tenantId)`, which invalidates that workspace's queries and the tenant list.
- `Resource` shows a loaded query as data with the refresh notice, else the error, else the loading status (`resourceState`).
- `useProposalKey` holds one UUID per exact JSON payload. The server replays equal `(proposer_membership, key)` with equal payload hash and rejects a changed payload (409 `idempotency_key_reused`).
- After login, logout or password change the caller re-reads `/api/session/` (`loadSession`) for the rotated CSRF token and session tag; the lane does not refresh them.
- Colours and spacing come only from `tokens.css` (and `tokens.ts` for SVG), enforced by the `pyracast/design-tokens` ESLint rule. Numbers, units and zoned times come from `lib/format`; status labels from `lib/vocabulary`. `AsyncButton` announces outcomes through the one `LiveRegion`. `lib`, `i18n` and `ui` import no feature, and a feature reaches another only through its top-level modules (`import-direction.spec.ts`).

## Tests

- `web/tests/transport.spec.ts`, `web/tests/api-errors.spec.ts`, `web/tests/query-keys.spec.ts` (keys, pages, cyclic or foreign `next`), `web/tests/query-layer.spec.ts` (hidden tab makes no request, failing poll keeps the chart), `web/tests/workspace.spec.ts`.
- pytest: `tests/test_error_contract.py`, `tests/test_session_serialization.py`, `tests/test_session_tag.py`, `tests/test_account_security.py` (activity refresh).
- Gaps: no test sends a request past the 95 s timeout.
