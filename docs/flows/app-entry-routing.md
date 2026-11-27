# Open the app: session bootstrap and routing

A browser opens `/app/` (bookmark, public site, account or notification mail, administration link or redirect, social-provider return). The page fetches the session projection from `GET /api/session/` and shows one screen: the launch card (loading or its failure), sign-in/sign-up, or, once signed in, a resolved destination: the account page for a provider return, invitation acceptance, a hand-off to `/admin/` for MFA-verified platform administrators, or the plants of a chosen workspace. The choice is made in the browser from the session, the URL, `sessionStorage` and the workspace list; the server only reports who the caller is. A document shows one session: when another one is observed, it reloads.

## Entry points

- `GET /api/session/` - `pyracast.api.session.SessionView` (a public route of `RoutePolicy`, `never_cache`). Actor: anyone, anonymous or signed in.
- `POST /api/session/language/` - `SessionLanguageView` (`csrf_protect`); used here only for the `?language=` hand-off, owned by `i18n-and-language`. The client sends it only for an anonymous caller whose session language differs, and removes `?language=` from the URL once resolved.
- `web/src/app/app/layout.tsx` `AppLayout` (dynamic via `connection()`) mounts `web/src/features/session/app-root.tsx` `AppRoot` around every `/app/` screen: session bootstrap, the hand-off of the figures saved on the device, language hand-off, the session language's catalog (`i18n-and-language`), focus and idle reads, the session cover, the sign-out state, the document reload and `LaunchCard` (the brand as level-one heading and one `role="status"` around the progress bar; every loading screen of the entry). `app/app/page.tsx` `AppPage` renders `features/session/entry.tsx` `Entry`, which signs in or resolves the destination (`chooseWorkspace` in `web/src/features/workspace/workspace-choice.ts`); the destinations (`/app/account/`, `/app/invitation/`, `/app/w/`, `/app/w/<tenant>/...`) are separate pages under `RequireUser` (`features/session/require-user.tsx`).
- Inbound links: `HEADLESS_FRONTEND_URLS` (`/app/`, `/app/?signup=1`); provider return `/app/?social_callback=1` (`social_callback_url`); invitation mail `/app/invitation/#token=`; `PlatformAdminSite.login` `/app/?next=<console path>`; administration menu `/app/w/` and `/app/account/`; admin logout `/app/`; notification mail `/app/w/<tenant>/...`; public site `/app/?language=` and `?signup=1`.

## Sequence

```mermaid
sequenceDiagram
    actor U as Browser
    participant D as AppRoot and Entry
    participant M as Middleware
    participant V as SessionView
    participant DB as PostgreSQL
    participant A as Admin site
    U->>D: "open /app/ with language, signup, social_callback, next"
    D->>M: "GET /api/session/ (one shared in-flight read)"
    M->>DB: "advisory lock, then django_session, auth_user, language preference"
    alt lock wait over 5 s
        M-->>D: "429 session_busy, Retry-After 1, one client retry"
    else idle over 30 days, age over 90 days or markers missing
        M->>DB: "logout flushes django_session"
        M->>V: "anonymous request"
        V-->>D: "user null, csrf_token, language"
    else ok
        M->>DB: "activity marker and usersessions_usersession (at most every 60 s)"
        M->>V: "authenticated request with LANGUAGE_CODE"
        V->>DB: "mfa_authenticator (staff superuser only), language preference"
        V-->>D: "user with can_admin, mfa_required, csrf_token, language, expires_at, idle_seconds, session_tag"
    end
    opt "valid ?language= differs and caller anonymous"
        D->>V: "POST /api/session/language/ with X-CSRFToken, sets pyracast_language cookie"
        D-->>U: "on failure: language error and Try again"
    end
    opt "signed in and the device flags a saved copy or a removal"
        D->>D: "hand the saved figures over (2 s); on failure: launch card with Try again, nothing published"
    end
    alt user is null
        D-->>U: "Auth screen, register when signup=1, provider return completes here"
    else social_callback present
        D-->>U: "full navigation to /app/account/ with the query"
    else invitation token stored
        D-->>U: "/app/invitation/, AcceptInvitation"
    else can_admin
        D->>A: "location.replace /admin/ or the validated next"
    else
        D->>V: "GET /api/tenants/ (launch card meanwhile)"
        D-->>U: "replace once to /app/w/<tenant>/ (remembered or first), or /app/w/ without a workspace"
    end
    D->>M: "GET /api/session/ again on focus, tab visible, idle mark, sign-in or sign-out"
    opt "an answer shows another session than the document's"
        D-->>U: "launch card, then reload of the same address and fragment"
    end
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `django_session` | R, W | Session load; `SessionLifetime` rewrites `last_activity_at` at most every 60 s; a session without a tag gets one at its first read; `logout` flushes an expired session |
| `auth_user` | R | User, `is_staff`, `is_superuser` for `can_admin` and `mfa_required`; `has_usable_password` |
| `mfa_authenticator` | R | `has_mfa_evidence`, only for staff superusers: a live TOTP or recovery-code row named in the session's authentication records, of any age |
| `accounts_userlanguagepreference` | R | `RequestLanguage` and `language_preference`; not written here (the hand-off POST is sent only for anonymous callers) |
| `usersessions_usersession` | R/W | `record_activity` upserts the device row when the activity marker is refreshed (every 60 s) or the session has no device marker yet; `revoke_other_sessions` finds sessions through these rows |

## Authorization

- `SessionView` is public by design: it returns `user: null` instead of 401/403 and exposes only the `SessionUserOutput` projection. Routing to workspace, invitation or admin is client-side and carries no authority; tenant endpoints enforce membership.
- `can_admin` is recomputed per request (`has_platform_admin_session`: active staff and superuser with MFA evidence). It only steers navigation; `PlatformAdminSite.has_permission` re-checks on every console request.
- CSRF: the GET issues a masked token (`get_token`); the cookie is HttpOnly and never read by the browser. `SessionLanguageView` is wrapped in `csrf_protect`. Responses under `/api/` are `private, no-store` (`PrivateAPIResponses`).
- `SessionTagCheck` refuses any request except the session read that carries an `X-Session-Tag` other than the one the session holds: 409 `session_changed`, before any view runs. The tag is an observation, never a credential.

## Failure paths

| Condition | Result |
| --- | --- |
| The launch card's session read fails (the first read, its "Try again", or the read after another tab observed another session; network, 4xx/5xx, or 429 `session_busy` after its one retry) | Launch card with the error, localized from the answer's `code` and `message`, "Try again" (`retry` reads again) and the link to the start address; the catalogue and language failures show the same card titled "Pyracast could not be opened" |
| The saved-figures hand-off fails, is refused, meets another database version, is unavailable or does not finish in 2 s | Nothing is published: launch card with `Bootstrap.savedFiguresUnchecked` and "Try again", which reads the session and hands over again; only a browser whose flag cannot be read and whose `indexedDB.open` is refused synchronously (or absent) goes on without it |
| Workspace list read fails at `/app/` | Once the read the mounted entry started has answered and before it sent its navigation: the card's failure with "Try again" (reads the list again) and the link to the start address. After the navigation is sent the entry shows only the loading card for as long as it stays mounted: no failure, no second navigation, also when the list changes or the navigation ends on `/app/` |
| Language hand-off POST fails | `Language.failed` notice with retry; sign-in stays hidden and focus reads wait until it succeeds |
| Session idle 30 days, older than 90 days or lacking lifetime markers | Server logs out; the next session read returns `user: null` and the Auth screen shows |
| Expiry, a sign-out here or in another tab, another user, a new tag of the same user, or a sign-in completed on this document's sign-in screen | A session read shows a session other than the one the document's first read found (a signed-in document takes an answer without a user only when a second read sent after it agrees): the screens give way to the loading state and the same address reloads with its fragment, or the workspace address of a confirmed provider sign-in (flow `social-sign-in`). From then on the document sends no tagged request: `session_changed`, or `NotSentError` after its own confirmed sign-out. The fresh document compares its first answer with the owner this tab last showed (`sessionStorage` `pyracast.shownOwner`): when that record differs and no sign-out began in this tab, it shows "session expired" on sign-in (no user) or `Errors.sessionChanged` in the shell (another owner) once; without the record (a blocked or fresh `sessionStorage`) it shows neither |
| Restored from the back-forward cache (`pageshow` with `persisted`) | Loading state, then a reload |
| Focus refresh fails | The screen stays; once the last confirmation is as old as the session's `idle_seconds` the idle rule applies |
| Session busy at the `/admin/` hand-off | 503 no-store page with `Retry-After: 1` that reloads once |

## Rules and invariants

- Destination order for a signed-in `/app/`: a provider return (`social_callback`, with or without `error`) goes to `/app/account/` with its query, where `LinkedAccounts` consumes it; else a stored invitation goes to `/app/invitation/`; else an administrator to `/admin/`; else the entry reads `GET /api/tenants/` under the shell's query key and replaces once to `/app/w/<tenant>/` of the workspace remembered in `localStorage` `pyracast.workspace` if still listed, else of the first row; with no workspace to `/app/w/` (the empty state). It chooses only from a settled list: a list cached within the 2 s `staleTime` is used at once, an older one is read again first. One navigation is dispatched per mounted entry. `WorkspaceIndex` still redirects at `/app/w/` opened directly. `?next=` counts only for an administrator, only without surrounding space, backslash or unprintable character, and only when it resolves to a same-origin `/admin/` path (else `/admin/`); the server validates the same address (`console_path`) when a provider sign-in carries it. `appQuery` in `web/src/lib/routes.ts` is the one parser of `signup`, `language` and the provider return (`social_callback`, `error`); `addressWithout` strips them from the address; `localPath` checks both the console return and the provider sign-in's workspace return.
- A provider sign-in started at an address under `/app/w/` returns there (flow `social-sign-in`): the return address replaces `/app/` just before the document reloads for the new session, unless a stored invitation wins.
- An administrator reaches the customer workspace through `/app/w/` or `/app/account/` directly; nothing persists a choice. A staff or superuser without MFA evidence gets `can_admin: false` and the customer workspace.
- The invitation token is read from the fragment of `/app/invitation/`, stored in `sessionStorage` (`pyracast.workspaceInvitation`) and stripped from the URL. It is cleared by the fresh document whose first answer shows another owner than the signed-in one this tab recorded in `pyracast.shownOwner` (also after a back-forward restoration or a manual reload; without that record it stays), except after "Use another account" (`signOut({ keepInvitation: true })`), which ends in a fresh document at `/app/invitation/`; leaving a pending sign-in stage keeps it, and accepting or dismissing clears it (`clearWorkspaceInvitation`). A blocked `sessionStorage` keeps it for the current load only. No other entry address carries it: `/app/#token=`, `?token=`, `?account=`, `?workspace=` and `/invitations/accept/` are not read.
- One `GET /api/session/` is shared while no session change was sent and no other tab observed another tag; a read is used only while the observed tag and `pyracast.sessionRevision` (a random value written at every change of the observed tag) are what they were when it started; requests to `/api/session/` and `/api/auth/` run one after another, so Strict Mode and focus events share one CSRF handshake. The client retries a `session_busy` answer once after `min(retry_after, 2)` seconds.
- Session limits are enforced server-side (`SessionLifetime`): idle 30 days, absolute 90 days (flow `session-and-csrf`); the workspace list poll (120 s) counts as activity. Every request carrying a well-formed session cookie (32 lowercase alphanumerics, existing or not) waits up to 5 s for a PostgreSQL advisory lock and holds it for the whole request, this one included; a provider callback with valid state releases it before it exchanges the provider's code. The clock is compared every 30 s and on visibility: the shell warns five minutes before `expires_at`. The last confirmation is the send time of the newest answer that confirms the session: a session read this document adopted, or a workspace-list read that the mounted workspace shell sees answered by the network (a cached list or a refusal does not count). A visible signed-in window whose last confirmation is as old as the session's `idle_seconds` or more, or later than the clock, reads the session every 30 s; a read that fails then covers the screens (kept mounted, `hidden` and `inert`) with "Could not confirm your session" and Retry, until an answer whose request was sent less than `idle_seconds` ago and not later than the clock, plus "Show saved figures" (a page reload) while the device's saved copy is on.
- A signed-in session answer is published (adopted, its tag observed, the language hand-off ready) only after `handOff` in `lib/session-client.ts` ended handed over: no flag (the store is not loaded), this user's holder renewed or activated, the copy cleared, or no database. A holder of another user, or none, is removed with `plants` and `days` in the hand-off's transaction; the device's marks (each person's "off", each ended session) stay. The holder is activated for a new session only when no mark refuses the person or the session. A copy confirmed more than 90 days ago or more than a day ahead of the clock is emptied. Every store operation runs under one 2 s deadline from the moment the document asks for it (`lib/saved/deadline.ts`). Only a withdrawn build loads the store at a document's start, to delete the copy where `localStorage` `pyracast.saved` is set or cannot be read. The hand-off holds the Web Lock `pyracast.saved-copy`; a failed hand-off shows "The figures kept on this device could not be checked." with Try again, and no idle confirmation read starts while it is shown (flow `saved-figures`).
- Route changes a person starts go through `useNavigate` (flow `frontend-data-layer`): while unsaved input is registered, or for good once the document asked before unloading, they load a document instead of a soft change; the entry's own redirects keep the router.

## Tests

- pytest: `tests/test_session.py` (projection, `can_admin`, `mfa_required`, cache headers), `tests/test_session_tag.py` (tag, refusal of another session's request), `tests/test_language.py` (language resolution, CSRF), `tests/test_session_serialization.py` (lock, 429 `session_busy`), `tests/test_session_lifecycle.py` (limits, activity written once a minute), `tests/test_account_security.py` (invalid markers, `expires_at`), `tests/test_console_failures.py` (busy page of the console).
- Playwright: `web/tests/launch.spec.ts` (the request sequence and screens of a signed-in and a signed-out launch, the card's accessibility, the list failure, the remembered, left and missing workspace, a stored invitation, a list that changes or fails while the navigation is held, a navigation back to `/app/`, a list error in the cache), `web/tests/saved-copy.spec.ts` (the hand-off of a real IndexedDB copy, removal at sign-out and at the next start, the deadline), `web/tests/workspace.spec.ts` (one CSRF handshake), `web/tests/localization.spec.ts` (language hand-off, focus reads), `web/tests/auth-registration.spec.ts` (administrator hand-off, `next` validation), `web/tests/app-routes.spec.ts` (addresses; `/invitations/accept/` answers 404 and `/app/` ignores a token, account or plant in its query or fragment; invitation token), `web/tests/session-document.spec.ts` (one document per session: expiry, sign-out here and elsewhere, another user, a new tag, sign-in on the sign-in screen, back-forward restoration), `web/tests/session-clock.spec.ts` (the ending-soon warning and the idle cover), `web/tests/session-tabs.spec.ts` and `web/tests/session-tag.spec.ts` (cross-tab sessions), `web/tests/social-auth.spec.ts` (provider return to the account page), `web/tests/transport.spec.ts` (`session_busy` retry).
