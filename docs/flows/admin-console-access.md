# Platform console access: MFA gate, SPA hand-off, confirmation, shared shell and logout

A platform administrator reaches `/admin/` either through the SPA hand-off (`can_admin` in the session projection) or by opening a console URL directly. Django admits the request only for an active staff superuser whose session holds TOTP or recovery-code evidence for a live authenticator; everyone else is bounced to the SPA. Every write needs a further, recent confirmation with an authenticator code. The outcome is a server-rendered console page inside one shared shell, or a redirect to `/app/`.

## Entry points

- `GET|POST /admin/login/` - `pyracast.platform_admin.site.PlatformAdminSite.login` (`never_cache`, not behind the gate). Actor: anyone. The return address `next` is kept only when `navigation.console_path` accepts it (same origin, under `/admin/`, no backslash, unprintable, empty or dot segments), else `/admin/`. A permitted administrator goes to it; an anonymous visitor to `/app/?next=<it>`; a signed-in account without console access to `/app/`, with an `authorization_denied` line (`platform_admin_required`, view `admin/login/`). A POST body is ignored but still needs a CSRF token.
- Every other console route (workspace pages, record browser, `jsi18n/`, the catch-all) is built by `PlatformAdminSite.admin_view`: Django's gate (`has_permission` = `authorization.has_platform_admin_session`), then `confirmed` for unsafe methods, then `failures.contained`.
- `GET /admin/` - `PlatformAdminSite.landing`: follows once a console path a provider sign-in stored in the session (`CONSOLE_RETURN`, checked again with `console_path`), else renders the overview.
- `GET|POST /admin/confirm/` - `pyracast.platform_admin.access.confirm`: asks for an authenticator or recovery code (`ConfirmationForm`) and returns to `next` (console paths only).
- `POST /admin/logout/` - `access.logout` (`auth.logout`, then `/app/`); outside the gate, so any session can end itself.
- `POST /admin/language/` - `access.language` (shell switcher; stores the account preference and the language cookie, returns to `next`).
- `GET /admin/password_change/` - `RedirectView` to `/app/account/`; there is no console password form.
- SPA: `web/src/features/session/entry.tsx` `Entry` (at `/app/`, for `can_admin` it does `window.location.replace` to the checked `next` or `/admin/`) and the `Administration` link in `web/src/features/workspace/workspace-shell.tsx` when `can_admin`.
- Shell: `src/pyracast/regions/templates/admin/workspace/base.html` (links "Account security" to `/app/account/` and "Customer workspace" to `/app/w/`), `admin-workspace.css`, `admin-workspace.js`.
- Operator: `pyracast bootstrap_admin` creates the administrator and its TOTP factor (`platform-admin-bootstrap`).

## Sequence

```mermaid
sequenceDiagram
    actor U as Admin
    participant S as SPA app
    participant D as Django
    participant DB as PostgreSQL
    U->>D: "GET /admin/plants/"
    D->>DB: "session advisory lock, django_session, auth_user, mfa_authenticator"
    alt active staff superuser, MFA record names a live authenticator
        D-->>U: "200 console page on base.html"
    else anonymous, customer, staff only or no MFA evidence
        D-->>U: "302 /admin/login/?next=/admin/plants/"
        U->>D: "GET /admin/login/?next=/admin/plants/"
        D-->>U: "302 /app/?next=/admin/plants/"
        U->>S: "open /app/, sign in with password and TOTP"
        S->>D: "POST /api/auth/browser/v1/auth/login, then auth/2fa/authenticate"
        D->>DB: "store mfa authentication record in django_session"
        S->>D: "GET /api/session/"
        D-->>S: "can_admin true"
        opt no social_callback or stored invitation token
            S-->>U: "location.replace /admin/plants/"
        end
    end
    U->>D: "POST /admin/settings/ without a recent confirmation"
    D-->>U: "403 confirmation page, nothing stored"
    U->>D: "POST /admin/confirm/ with a code"
    D->>DB: "lock account rows, check code, record reauthentication, rotate session key"
    D-->>U: "302 back to the console page"
    U->>D: "POST /admin/logout/ with CSRF token"
    D->>DB: "logout() deletes the django_session and device rows"
    D-->>U: "302 /app/"
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `django_session` | R/W | Session and allauth `account_authentication_methods` records (MFA evidence and its `reauthenticated` marks). `SessionLifetime` refreshes `last_activity_at` at most once a minute; a confirmation rotates the key; `logout()` deletes the row. |
| `usersessions_usersession` | R/W | Device record (ip, user agent, `last_seen_at`) written by the same refresh and rotated by a confirmation; logout deletes the session's row. |
| `auth_user`, `account_emailaddress`, `socialaccount_socialaccount` | R | Role flags are read live per request. A confirmation and the language switch lock the account (`lock_account`, `select_for_update`). |
| `mfa_authenticator` | R/W | Evidence counts only while its authenticator id still exists for the same user; a spent recovery code is marked used. |
| `accounts_userlanguagepreference` | R/W | `RequestLanguage` reads it on every authenticated request; `POST /admin/language/` upserts it. |

## Authorization

- Console gate: `has_platform_admin_session` runs on every `admin_view` request, `confirm/` included, and again inside `each_context`. Role flags and the authenticator row are read live, so demotion or factor removal applies on the next click.
- MFA evidence (`security.has_mfa_evidence`): an active user and a session record bound to this user with method `mfa`, type `totp` or `recovery_codes`, whose id names a current authenticator of the user. It exists only after a completed MFA sign-in or MFA reauthentication.
- Confirmation: every console request that is not GET, HEAD or OPTIONS, language switch included, needs `has_mfa_evidence(recent=True)`: a reauthentication by an enrolled authenticator within `ACCOUNT_REAUTHENTICATION_TIMEOUT` (300 s); a sign-in never counts. A change of the administrator's credential state (password, address, identity or authenticator) ends confirmations given before it. Only sign-out and `confirm/` are exempt. The code is checked under the account's locks (`processes.confirmation`).
- Console writes of business data take no account locks: one that passed its check before a credential change committed completes, and the next request is refused. No console route writes an account's credentials.
- Login side: `AuthenticationBackend` and `SocialAccountAdapter` refuse staff or superusers without TOTP (`platform-admin-bootstrap`).
- SPA: `SessionUserOutput.get_can_admin` uses the same predicate and only steers navigation; domain operations the console calls re-check `require_platform_admin` (active staff superuser, no MFA). `/admin/login/` and `get_can_admin` must keep equal rules, or `/admin/` and `/app/` would redirect to each other.
- CSRF: `CsrfViewMiddleware` rejects a POST without token with 403 before the gate runs, `/admin/login/` and `/admin/logout/` included.

## Failure paths

| Condition | Result |
| --- | --- |
| Anonymous, customer, staff without superuser, superuser without MFA evidence, inactive | 302 `/admin/login/?next=<path and query>`, then 302 `/app/?next=<path>` (anonymous) or `/app/` (signed in without access); POST bodies discarded |
| Unsafe method on any gated route without recent confirmation | 403 confirmation page (HTML, also for scripts), nothing stored |
| `confirm/` with a wrong code | 400, the form again; a valid code answers 302 to `next` with a success notice; other methods 405 |
| Logout or language via GET | 405 (logout for any identity; language after the gate) |
| POST without CSRF token | 403 |
| Language outside `en`, `tr` | 400 empty body |
| `next` missing, not same-origin or not under `/admin/` | Falls back to `/admin/` |
| Idle over 30 min, age over 12 h or session markers missing | `SessionLifetime` records `session_expired` and calls `logout()`; the request is anonymous (first row) |
| Same session holds the advisory lock over 5 s | 503 HTML page with `Retry-After: 1`; a GET without `session_retry` also reloads itself once; a write is never replayed |
| Retryable database conflict or timeout on an unsafe request | Redirect to the referring console page with a retry notice (503 JSON with `Retry-After: 1` when `Accept: application/json`); other database errors stay 500 |
| Admin signs in with a stored invitation token or a provider return | Invitation screen or account page, no hand-off |

## Rules and invariants

- The console has no login form. `?next=` is validated twice (`console_path` server side, `consoleDestination` in `entry.tsx`); the hand-off applies only to the page load that has none of the provider or invitation markers.
- MFA proof lives and dies with the session (30 min idle, 12 h absolute). Every `logout()` emits the `logout` security log event.
- `SessionRequestSerialization` takes a PostgreSQL advisory lock per well-formed session cookie, so requests of one session run one at a time.
- The stock model routes stay behind the same gate on Django templates (`admin-record-browser`); the default `django.contrib.admin.site` is empty and `password_change/done/`, `autocomplete/` and the `regions/region/<id>/setup/` alias are unrouted.

## Tests

- `tests/test_console_navigation.py` (deep link, hostile return addresses, uncached login, refusal line, sign-out for any identity), `tests/test_provider_console_return.py`, `tests/test_console_step_up.py` (stale evidence refused for every route and unsafe method, confirmation, return only to console pages, language needs confirmation and sign-out does not), `tests/test_session_authority.py` (confirmation under the locks), `tests/test_console_failures.py` (busy page, retry message).
- `tests/test_admin_workspace.py` (gate, logout), `tests/test_platform_admin_agreement.py`, `tests/test_account_security.py` (no independent password entry, MFA evidence bound to the live authenticator), `tests/test_session.py`, `tests/test_language.py`, `tests/test_admin_language_controls.py`.
- Playwright: `web/tests/auth-registration.spec.ts`, `web/tests/responsive.spec.ts`, `web/integration/admin-csp.spec.ts` (live credentials: sign-in, navigation, language, mobile-menu sign-out).
- Gaps: browser coverage of the `social_callback` and invitation exemptions of `Entry`, and of console behaviour after session expiry.
