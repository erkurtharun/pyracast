# Connect or disconnect a social account

A signed-in user opens Account settings, lists the Google or Microsoft identities linked to the account, links another one through the provider redirect, or removes one. Connecting needs a recent confirmation with the account's strongest factor before the redirect and again when the connection completes; both changes run in one transaction that locks the account's rows, and both send a notice mail and a security log line.

## Entry points

- Screen: `web/src/features/account/linked-accounts.tsx` `LinkedAccounts`, mounted by `account-security.tsx` `AccountSecurity` on `/app/account/`. Buttons: `features/auth/social-provider-buttons.tsx` `SocialProviderButtons` with `process="connect"` (providers from allauth `GET config`), shown only after a confirmation through the shared prompt (`features/account/step-up.tsx`, factor from `GET account/authenticators`: authenticator, else password, else emailed code). The page consumes the provider return (`?social_callback=1`, `error`, `error_process`) once and sends `POST auth/provider/complete`.
- `GET|DELETE /api/auth/browser/v1/account/providers` -> `pyracast.accounts.social_views.ManageProvidersView` (list, disconnect).
- `POST .../auth/provider/redirect` (native form post, `process=connect`) -> `social_views.RedirectToProviderView`, shared with social sign-in; then `GET /api/accounts/{google,microsoft}/login/callback/` -> `social_views.provider_callback` (`ProviderCallbackView`, verifies only); then `POST .../auth/provider/complete` -> `CompleteProviderLoginView`, `social_views.connect` and `social.connect_identity`.

## Sequence

```mermaid
sequenceDiagram
    actor U as User
    participant L as LinkedAccounts
    participant V as API views
    participant D as PostgreSQL
    participant P as Provider
    L->>V: "GET account/providers"
    V-->>L: "200 accounts of request.user with can_disconnect and sign_in_available"
    U->>L: "Connect an account, confirm with the named factor"
    L->>V: "form POST auth/provider/redirect process=connect"
    V->>D: "BEGIN, lock account rows, check session row and authority"
    alt "callback_url differs, CSRF form expired or confirmation stale"
        V-->>U: "302 app with error=invalid_request, expired_form or reauthentication_required"
    else "accepted"
        V->>D: "state with user_id saved in the session, COMMIT"
        V-->>U: "continuation page, then provider"
    end
    U->>P: "sign in and consent"
    U->>V: "GET callback with code and state (provider redirect)"
    V->>D: "take state, release the session lock"
    V->>P: "token and profile requests, no lock, no transaction"
    V->>D: "store the verified login for this session (authentication cache)"
    V-->>U: "302 /app/?social_callback=1, or with error"
    L->>V: "POST auth/provider/complete"
    V->>D: "take the stored login, once"
    alt "none stored"
        V-->>L: "409"
    else "signed out"
        V-->>L: "401 not authenticated"
    else "other user than the initiator"
        V-->>L: "401 step-up naming the factor"
    else "locked"
        V->>D: "BEGIN, lock account rows, check session row and authority"
        alt "authority or confirmation stale"
            V-->>L: "401 step-up naming the factor"
        else "identity owned by another account"
            V-->>L: "400 connected_other, nothing written"
        else "linked"
            V->>D: "INSERT socialaccount, mail intent, COMMIT, log line"
            V-->>L: "200 accounts"
        end
    end
    L->>V: "DELETE account/providers"
    V->>D: "BEGIN, lock account rows, validate_disconnect"
    alt "no other login method"
        V-->>L: "400 disconnect_last"
    else "stale authority or confirmation"
        V-->>L: "401 step-up, prompt, replay once"
    else "removed"
        V->>D: "DELETE row, end other sessions, mail intent, COMMIT"
        V-->>L: "200 remaining accounts"
    end
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `socialaccount_socialaccount` | R/W | Listed per user. The completion reads the identity's owner by `(provider, uid)` without a lock; insert on connect, `extra_data` (email only) refreshed for an identity the account already holds, delete on disconnect. Unique `(provider, uid)`. |
| `auth_user`, `account_emailaddress`, `mfa_authenticator` | R, lock | `account_state(lock=True)` locks the account's addresses, identities, user row and authenticators in that order for every connect redirect, completion and disconnect; `SocialLogin.connect` writes the locked user row back as is; `has_usable_password` and verified addresses feed `can_disconnect`. |
| `django_session` | R/W | OAuth state (`socialaccount_states`, allauth's state lifetime), authentication records, the authority record `pyracast_session_authority`; disconnect deletes the other sessions of the account (with `usersessions_usersession`). |
| `pyracast_authentication_cache` | R/W | The verified login of the callback, keyed by a digest of the session key, kept for allauth's state lifetime and taken once. |
| `notifications_outboundmail` | W | The connect or disconnect notice, stored as a mail intent with the write. |
| `socialaccount_socialapp`, `socialaccount_socialtoken` | none | Apps come from environment credentials (`list_apps`); tokens are discarded. |

## Authorization

- Session and CSRF: `CsrfViewMiddleware` (form field or `X-CSRFToken`); the account views answer 401 when anonymous.
- Confirmation (300 s, `ACCOUNT_REAUTHENTICATION_TIMEOUT`): `RedirectToProviderView` (connect), `social_views.connect` (again at the completion) and allauth's disconnect flow. A confirmation accepts only the account's strongest factor, and the 401 names that flow (`step-up-reauthentication`). The session's authority record must equal the account's locked credential state, else the 401 comes whether or not the request carried a proof.
- Ownership: allauth's `DeleteProviderAccountInput` finds only accounts of `request.user`; the callback state carries `user_id`, which must equal the session user at the completion. `callback_url` is pinned to `social_callback_url()`; `provider_callback` refuses unconfigured providers. Unique `(provider, uid)` is the last guard against two users claiming one identity.

## Failure paths

| Condition | Result |
| --- | --- |
| Anonymous GET or DELETE | 401 `is_authenticated` false, UI refreshes the session, `sessionExpired` |
| Wrong CSRF token | 403 `csrf_failed` JSON; on the native form post 302 `error=expired_form` |
| Disconnect or completion without a current confirmation or authority | 401 naming the strongest factor's flow; shared prompt, then the disconnect replays once and a connection shows the provider buttons again |
| Last sign-in method (no password with a verified address, no other enabled provider) | 400 `disconnect_last`, answered before the confirmation check; the button is disabled by `can_disconnect` |
| Identity held by another account | 400 `connected_other`, shown above the list; a pending provider sign-up stays |
| No stored login (expired, used, or its session ended) | 409; generic `callbackError` notice |
| Session ended during the write | 400 `session_ended` |
| Session lock wait over 5 s (redirect, callback, completion, list) | 429 `session_busy` JSON, one client retry for fetches |
| Provider not configured; cancelled at the provider, provider or network error; missing or used state | 302 `error=provider_unavailable` (no `error_process`); `error=cancelled` or another code with `error_process`; `error=unknown` with `error_process=login` when no state is found. Every connect error shows one generic `callbackError` notice; `error_process` is not read |
| Notice mail cannot be sent | The mail worker retries the stored intent; the committed change stays and the request is unaffected (`account-mail-notifications`) |

## Rules and invariants

- Lock order: per-session advisory lock (`SessionRequestSerialization`), then the account's rows (`credential_write`: addresses, identities, user, authenticators, each in id order), then the session row. The callback releases the session lock before the provider exchange and holds no transaction; another account's rows are never locked by a connection.
- A password change, new address or authenticator, deactivation or other credential change during the provider round trip changes the account's state, so the completion answers the step-up 401 and stores nothing. The 300 s window is shorter than the state lifetime: a slow provider screen ends in that 401.
- Connect and disconnect change the account's credential state, so every pending code or sign-in step of the account ends and other sessions' confirmations stop counting. Disconnect also ends the other sessions (`sessions_revoked`, reason `social_account_removed`); connect does not.
- Disconnect needs a usable password with a verified address, or another identity whose provider is still enabled. Identities of a disabled or retired provider stay listed (without sign-in flows, `sign_in_available` false) and removable but do not count as a login method.
- Reconnecting an owned identity only refreshes `extra_data`: no insert, mail or log line. Audit is two JSON log lines (`social_account_connected`, `social_account_disconnected`) written with the commit, no table; a refused connection writes `social_login_refused` with its reason.

## Tests

- `tests/test_social_accounts.py` (link, unlink, audit, last-provider rule, disabled provider removal, concurrent disconnect, callback state and URL, provider errors, disconnect rechecking authority after the lock), `tests/test_provider_completion.py` (callback verifies only, completion refusals, rollback leaves no line or mail, own identity refresh), `tests/test_provider_identities.py` (disconnect ends other sessions, unknown provider listed), `tests/test_account_settings_answers.py` (`can_disconnect`, `sign_in_available`), `tests/test_provider_callback_contract.py` (pins the allauth sequence the callback re-implements), `tests/test_social_refusal_audit.py`, `tests/test_account_mail_failures.py`; `web/tests/social-auth.spec.ts` (confirmations, callback error consumed once, `connected_other`), `web/integration/social-auth.spec.ts` (connect up to the provider redirect).
