# Frontend shared layer: HTTP client, queries, polling, forms and UI primitives

Every customer screen reads and writes through one typed client over one request lane. TanStack Query owns server state: `useApiQuery` and `useApiList` load saved data and, when the screen passes `poll`, re-read it without calling providers while the tab is visible. `ActionForm` and `AsyncButton` turn a rejected action into field-level or general messages, and a change whose answer was lost into an unconfirmed notice. The outcome is a screen showing saved data, a localized message, or a per-field error.

## Entry points

- `web/src/lib/api/client.ts` `api` (`get`, `post`, `put`, `patch`, `delete`): paths, parameters, query and body typed from the generated `web/src/lib/schema.d.ts`.
- `web/src/lib/api/lane.ts` `send`, `prepared`, `exchange`: the only `fetch`. CSRF token from the session store, `X-Session-Tag`, `/api/session/` and `/api/auth/` calls serialized, one retry for `session_busy`, 95 s timeout, `redirect: "error"`.
- `web/src/lib/api/errors.ts` `errorFromResponse`, `ApiError` with its subclasses `UnconfirmedError` (a change whose outcome is unknown) and `NotSentError` (refused before sending), `DisplayError`, `ERROR_CODES`, `answerLost`: one error value for every failure.
- `web/src/lib/auth-api.ts` `authRequest` for `/api/auth/browser/v1/*` (allauth headless), through the same lane.
- `web/src/lib/api/query.ts` `createQueryClient`, `useApiQuery` (takes `enabled`), `useApiList`, `useRefresh`, `readAgain`, `cadence`; keys in `web/src/lib/api/keys.ts`; `web/src/lib/session-client.ts` (session state, tag, `loadSession`).
- `web/src/ui/`: `form.tsx` (`ActionForm`, `AsyncButton`, `UnconfirmedNotice`, `WriteFailure`, `UnconfirmedTitle`, `useWriteErrorMessage`, field components), `resource.tsx` `Resource`, `ui.tsx` (`Notice`, `ReadFailed`, `RefreshFailed`, `Empty`), `action.tsx`, `live-region.tsx`; `web/src/lib/idempotency.ts` (`useAttempt`, `outstandingAfter`, `serverTrim`).
- `web/src/lib/leave-guard.ts`: one registry of unsaved input (`useLeaveGuard`), the `beforeunload` question and `useNavigate`; `web/src/instrumentation-client.ts` adds its `popstate` listener before hydration, so it runs before the router's.
- Styling: `web/src/styles/tokens.css` and `tokens.ts`, `web/src/app/globals.css`, enforced by `web/eslint/design-tokens.mjs`. The brand families (`--forest*`, `--amber*`, `--cream*`: the kit's three colours and their shades, and the accent `--teal*` for links, buttons, current items and focus accents) are defined once in `tokens.ts` (`brand`, `brandProperties`, only shades a stylesheet reads; `imageShades` for the social image) and published as custom properties on `<html>` by the root layout and `global-error.tsx`; `tokens.css` holds the other tokens, `--success` included.
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
        H-->>S: "first read: error with Retry; later: earlier answer kept with a refresh notice"
    end
    S->>H: "ActionForm submit"
    H->>L: "onSubmit: POST, PUT, PATCH or DELETE with X-CSRFToken"
    alt "400, 403, 409, 503 busy, 429 session_busy"
        L-->>H: "ApiError"
        H-->>S: "focused alert: field list, else message"
    else "no answer, unreadable 2xx, bare 502, 504 or 520 to 524, maintenance"
        L-->>H: "UnconfirmedError"
        H-->>S: "Change not confirmed: input kept, the same send repeats the attempt"
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
| Non-2xx with a body that lacks `code` and `message` | `Errors.request` with the status. A 2xx non-JSON body throws `Errors.invalidResponse` for a read |
| A change (non-GET) whose `fetch` rejects (also the 95 s timeout and any redirect), whose 2xx answer has no readable body (a 204 succeeds), or that is answered a bare 502, 504 or 520 to 524 or the `maintenance` refusal | `UnconfirmedError` (`answerLost`) with `Errors.unconfirmed`, headed "Change not confirmed"; while the form's live, valid, enabled input equals the attempt the text is `Errors.unconfirmedRepeatable`. Every other non-2xx stays an HTTP error. The body is serialized before `fetch`, so a body that cannot be written is neither sent nor unconfirmed |
| A tagged request while this tab's sign-out is pending (other than its DELETE) or after its confirmed sign-out retired the document | Not sent: `NotSentError` (status 0, `Errors.notSentSigningOut`), never taken for a lost answer |
| Contract body `{code, message, fields?, retry_after?, request_id?}` | One `ApiError` (status, code, fields, `retryAfter`, `requestId`, body); the server's localized message is shown as sent, followed by `Errors.reference` when it carries a `request_id` |
| 400 `invalid_request` with `fields` | `fields` become per-control errors in `ActionForm`; the general message stays beside them |
| 429 `session_busy` | The lane repeats the request once after `Retry-After` (at most 2 s), for every method; a second refusal is reported. `busy` (503) is never repeated |
| 409 `session_changed` | Repeated once only when a session read shows the same session under a renewed key; otherwise the caller gets a `session_changed` error |
| 401 `not_authenticated` or 410 `session_ended` on a non-session call | The lane reads the session once, then throws the error; the screen shows the sign-in |
| Connection failure, the 95 s timeout or a redirect on a read | `Errors.network`; a GET answered the maintenance refusal keeps its message. The reminder, CSV and invitation previews are POST reads (`READS` in `lib/api/client.ts`): a lost answer fails them as a read, with `Errors.network` (no answer), `Errors.invalidResponse` (unreadable 2xx) or `Errors.request` with the status (a bare gateway status or the maintenance refusal) |
| `next` repeats or leaves `/api/` or the origin | `Errors.invalidPage` |
| Read answers 403 or 404 | Stored as the query's answer (a refusal): no data, `denied`, the refusal's message; it stays through later failures until a later read is answered, so rows of a workspace the user lost never stay on screen. A later failed read keeps the data hidden and shows its own error instead of the refusal's message |
| First read fails | `ReadFailed`: the error and Retry, in `Resource` and on the screens that render their own state |
| Poll, focus read or refresh fails otherwise (no connection, 5xx, 401, unreadable body) | The last answer, rows or refusal, stays; rows show beside a `RefreshFailed` notice with their time and Retry. The client runs `networkMode: "always"`, so a read while offline fails instead of pausing, and `refetchOnReconnect` reads again on `online` |
| `authRequest` non-2xx | Returned only for allauth's 401 (body `status` 401 and `meta.is_authenticated` true, false or null) and the 404 on `GET account/authenticators/totp` (flow state); any other answer, another 401 included, throws through `errorFromResponse`. The server's `/api/` middleware gives an allauth error body the contract keys (`with_error_contract` in `pyracast.api.errors`: the first `errors[].message` as message, each `param` as a field), so the client reads those; a body that still lacks `code` and `message` gives `Errors.request` with the status |

## Rules and invariants

- `fetch` appears only in `lane.ts`, always `same-origin` and `no-store`. A `next` link is reduced to path plus query, so the host of a DRF absolute link is never followed.
- Session calls are serialized and a concurrent `GET /api/session/` is shared, so Strict Mode, focus and visibility events make one request.
- Query keys are `["session", "<user id>:<session tag>", workspace or account, path, parameters, query]`, without the language: two accounts, two sessions or two workspaces never share an entry, and a request is one query in every language. A document shows one session, so it never holds another owner's entries (flow `app-entry-routing`). When the shown language changes, `readAgain` marks every query stale and reads the active ones again; the screen keeps the previous answer in the previous language until the new one arrives.
- Polls run only in a visible tab and refetch when it becomes visible; a read never overlaps itself. Periods: `cadence.live` 30 s, `steady` 60 s, `background` 120 s. A list read over n pages polls every n periods. Queries do not retry on their own.
- Polls count as activity (`docs/ARCHITECTURE.md`), so a visible tab never hits the idle limit, only the 90-day one.
- A change refreshes through `useRefresh().workspace(tenantId)`, which runs `readAgain` on that workspace's queries and the tenant list: it awaits `cancelQueries` (a read not yet answered is reverted and never writes) and then `invalidateQueries`, which marks the queries in scope stale and reads the active ones again, so each ends with a read sent after the change. A screen that passes its own query (`mounted`) keeps a read of it already running, which it started after the change, instead of cancelling and repeating it. "Refresh list" in the inbox (`useRefresh().requests`) is a read the person asks for: `invalidateQueries` with `cancelRefetch: false`, which joins a running read. Retry calls `refetch({ cancelRefetch: false })`. `Loaded.reload()` resolves only with a value written after it started, never a refusal.
- `Resource` shows a loaded query as data with the refresh notice, else the error, else the loading status (`resourceState`).
- A form keeps one outstanding attempt (`useAttempt`): the request whose answer was lost. A send whose canonical body equals it reuses its key; any other send gets a new key. An answer to the attempt other than 429 ends it; an answer to other content leaves it. Texts are trimmed by `serverTrim` (Python's `str.strip()` set) before they are compared or sent. The server replays an equal key with an equal payload and rejects a changed payload (409 `idempotency_key_reused`).
- `ActionForm` and `AsyncButton` keep the typed error and derive its text at render, so a language change follows. `ActionForm` `attempt` renders the outstanding write's notice for as long as the owner keeps it and any other answer beside it; it reads the form's live data after every change and again before sending, and shows the repeatable text only while that data is valid, equals the attempt and its submit control is enabled. Screens with their own failure use `WriteFailure` or `UnconfirmedTitle` with `useWriteErrorMessage`.
- Unsaved input registers with the leave guard: `ActionForm` from its first change (or its `unsaved` flag) until its submission succeeded, while a submission runs or an attempt is outstanding (`guard={false}` on the sign-in form); the production editor while its values differ from those it opened with or its write runs or is unconfirmed; a staged proposal; an import while its chosen file is read, ready or previewed and not yet imported (not once the choice is invalid); the collection schedule while its times differ; the plant wizard from its first input, or while a create or restore runs, until it is saved. While anything is registered the browser asks before unloading; once it asked, the document stays marked for good. While anything is registered or the document is marked, `popstate` reloads the entry traversed to as a document, and `useNavigate()(href, mode)` changes the route by `location.assign` or `location.replace` instead of the router. Route changes a person starts go through `useNavigate` (`AppLink`, plant navigation, the shell and the screens), except the invitation screen's exits, which load a document (`location.replace`), and the production draft's return, which only rewrites the address (`replaceAddress`, unmounting nothing); only the automatic redirects of `features/session/entry.tsx` and `features/workspace/redirect.tsx` keep the router, and `web/eslint.config.mjs` refuses `next/link` and `useRouter` elsewhere in `web/src/ui`, `web/src/features` and `web/src/app`.
- After login, logout or password change the caller re-reads `/api/session/` (`loadSession`) for the rotated CSRF token and session tag; the lane does not refresh them.
- Colours and spacing come only from `tokens.css` and `tokens.ts`, enforced by the `pyracast/design-tokens` ESLint rule; `pyracast/brand-source` refuses a brand property declared anywhere but `tokens.ts`. Numbers, units and zoned times come from `lib/format`; status labels from `lib/vocabulary`. `AsyncButton` announces outcomes through the one `LiveRegion`. `lib`, `i18n` and `ui` import no feature, and a feature reaches another only through its top-level modules (`import-direction.spec.ts`).

## Tests

- `web/tests/transport.spec.ts` (also the outcome of every lost or bare answer), `web/tests/api-errors.spec.ts`, `web/tests/query-keys.spec.ts` (keys, pages, cyclic or foreign `next`, refusals, reading again after a change, `reload`), `web/tests/query-layer.spec.ts` (hidden tab makes no request, failing poll keeps the chart), `web/tests/connection-loss.spec.ts` (offline and online, first-read failures, retained refusals), `web/tests/unconfirmed-writes.spec.ts` (lost answers repeated once per screen), `web/tests/leave-guard.spec.ts`, `web/tests/design-tokens.spec.ts` (no other source writes a brand value; every numbered shade recomputed from the kit hue), `web/tests/workspace.spec.ts`.
- pytest: `tests/test_error_contract.py`, `tests/test_session_serialization.py`, `tests/test_session_tag.py`, `tests/test_account_security.py` (activity refresh).
- Gaps: no test sends a request past the 95 s timeout.
