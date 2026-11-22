# Reset a forgotten password by emailed code

An anonymous user on the sign-in screen asks for a reset code, receives it by email, then submits the code with a new password. The pending reset lives in the requester's Django session. On success the password is replaced, every session of the account ends and the user returns to sign-in (no automatic login).

## Entry points

- `web/src/features/auth/auth.tsx` `Auth`, modes `forgot` and `reset` (`features/auth/steps/submit.ts`): reached from "Forgot password?", or on mount when `GET auth/session` reports a pending `password_reset_by_code` flow (reload resume). The code stage names the address, shows an ended stage on a 409 and offers "Cancel password reset", which ends the process through `DELETE auth/session`. Client: `web/src/lib/auth-api.ts` `authRequest`.
- `POST /api/auth/browser/v1/auth/password/request` -> `pyracast.accounts.headless.RequestPasswordResetView` (a `StartingView`); `POST .../auth/password/reset` -> `ResetPasswordView` (wrapped in `require_POST` in `accounts/auth_urls.py`). Actor: anonymous. Both swap the input class (`RequestPasswordResetInput`, `ResetPasswordInput`) on allauth's headless views; code, attempts and state are allauth's `PasswordResetVerificationProcess`.
- Mail: `AccountAdapter.send_mail` stores an outbound intent (`account/email/password_reset_code`, or the anti-enumeration notice `unknown_account`; `account-mail-notifications`) that the mailer actor `send_outbound_mail` sends after the commit; the code expires with its intent after ten minutes. The completion notice `password_reset` is stored the same way. No command or script.

## Sequence

```mermaid
sequenceDiagram
    actor U as User
    participant UI as Auth screen
    participant V as Headless views
    participant DB as PostgreSQL
    participant W as Mailer worker
    U->>UI: "Forgot password, enter email"
    UI->>V: "POST auth/password/request"
    V->>DB: "advisory lock (session cookie only), one transaction: reset_password budget, recovery_identity locks the account rows"
    opt budget spent
        V-->>UI: "429, before any row is locked"
    end
    V->>DB: "code intent, or unknown_account intent when no eligible account; session state with email, user id, code, attempts, clock and account stamp saved before the commit"
    V-->>UI: "401 with flow password_reset_by_code pending"
    W->>DB: "after the commit, send the pending intent"
    U->>UI: "code and new password"
    UI->>V: "POST auth/password/reset"
    V->>DB: "reset_password_from_key limit, resume session state"
    alt limit exceeded, or no state, expired, aborted or account changed
        V-->>UI: "429, or 409"
    else wrong or missing code
        V->>DB: "failed_attempts + 1, drop state at 3"
        V-->>UI: "400 incorrect_code, or required for a missing or empty key"
    else password rejected
        V-->>UI: "400 on password, no attempt used"
    else code and password valid
        V->>DB: "one transaction: claim the address, check the account stamp under lock, save hash"
        V->>DB: "when the address is unverified, mark it verified and primary, sync auth_user.email"
        V->>DB: "end all sessions of the account, drop state, clear login_failed counters, queue password_reset notice"
        V-->>UI: "401 without errors or pending flow"
        UI-->>U: "login mode with resetDone notice"
    end
```

## Data

| Table | R/W | What |
|---|---|---|
| `django_session` | R/W | State under `account_password_reset_verification`: email, user id, plaintext code, `failed_attempts`, `at`, `code_confirmed`, account stamp. Removed on success, abort, or when any auth response resumes it expired. |
| `account_emailaddress` | R/W | `recovery_identity` lookup; a confirmed code marks an unverified matching address verified and primary. |
| `auth_user` | R/W | Success writes the hash (`update_fields=["password"]`); verifying an address also rewrites `email`. |
| `socialaccount_socialaccount`, `mfa_authenticator` | R/W, R | Locked with the account's other rows at the request and the completion as its credential state; the identities of an account that never signed in are removed on success. |
| `usersessions_usersession` | R/W | Rows of the account's sessions are deleted with them (`sessions_revoked`, reason `password_reset`). |
| `notifications_outboundmail` | W | Code or `unknown_account` intent; `password_reset` notice. |
| `pyracast_authentication_cache` | R/W | Rate-limit histories via the autocommit `auth_cache` connection; success clears the per-address `login_failed` counters (10 minute and daily) for all the user's addresses; the per-IP `login_failed_ip` count is kept. |

## Authorization

- No authentication. CSRF is enforced on both POSTs by `CsrfViewMiddleware` (token from `/api/session/`). The code exists only in the requesting session, so another browser gets 409.
- Eligibility is decided at request time in `RequestPasswordResetInput.clean_email` via `pyracast.accounts.authentication.recovery_identity`: either exactly one account owns the verified address and it is active, or none does and the lowest-id active account whose only address is this primary is chosen.
- `ACCOUNT_RATE_LIMITS` `reset_password` (`spent` before the account is locked, consumed after input validation) and `reset_password_from_key` (before anything else), consumed inside the allauth views. `SessionRequestSerialization` holds a per-session advisory lock whenever a session cookie exists, so parallel attempts cannot lose counts. No MFA at reset; TOTP is still required at the next login, and staff without TOTP cannot sign in (`AuthenticationBackend`).

## Failure paths

| Condition | Result |
|---|---|
| Missing CSRF token / non-POST method (including allauth's GET code check) | 403 `csrf_failed` / 405 |
| Malformed JSON, invalid email, missing field | 400 (`bad_request` for bad JSON, else `errors` with `param`); a missing `key` costs an attempt |
| Request limit 5/h IP or 3/h email, reset limit 10/h IP, or advisory lock wait over 5 s | 429; the reset limit counts every POST, including 409s and wrong codes; the lock gives `session_busy` with `retry_after` and `Retry-After: 1` |
| Email unknown, inactive, duplicated verified, or unverified but not the account's only address | Same 401 and pending flow as success; the mail is `unknown_account`; the stored state has no code, so every submitted code is `incorrect_code` |
| Reset without state or session, after 600 s, or after 3 failures | 409, no `errors` |
| Account changed since the code was requested (password, active flag, addresses, provider identities, authenticators) or deleted | 409, the state is dropped, nothing is mailed |
| Wrong code, or missing or empty code | 400 `incorrect_code` on `key`, or `required` on `key`; each costs an attempt and the third also aborts the state |
| Password under 15 characters, common, similar or over 1024 | 400 on `password`, state kept |
| Another account verified the address meanwhile | Verification is skipped silently (`can_set_verified`), reset still returns 401; a racing verification that violates `unique_verified_email` answers 409 and rolls the reset back |
| Mail cannot be delivered | The request answers the same 401 and pending flow for known and unknown addresses and keeps the code state; the intent is retried or ends `failed`, and a new request replaces a code that never arrived |

## Rules and invariants

- Code: 8 letters from `BCDFGHJKLMNPQRSTVWXZ` as `XXXX-XXXX` (allauth default), compared case-insensitively without punctuation, valid 600 s, 3 attempts; only a wrong or missing code costs one. A new request in the same session replaces its state; codes from other sessions for the same user stay valid until they expire or the account's credential state changes.
- A pending reset rests on its account's credential state (`processes.account_state`: password, active flag, addresses, provider identities, authenticators), read under lock where the request identified the account; any change ends it and a state equal again counts as the same. The request's transaction opens before the address is validated, so a change started while the code is requested waits and then ends the code.
- `ResetPasswordView.post` runs claim, stamp check and `super().post` (save password, `confirm_code`, `finish`) in one transaction; any failure of the completion (`AuthorityChangedError`, a row gone, a stale unique rule, a deadlock) drops the state and answers 409.
- A reset proves mailbox ownership: an unverified signup address becomes verified and a squatter's password is replaced. With several unverified claimants only the lowest active user id can recover. An account that never signed in loses its provider identities.
- Every session of the account ends through the `password_reset` signal (`sessions_revoked`).
- Every accepted request is the same 401 and stores one mail intent in the request's transaction, in the saved language of the verified address's owner, else the request language. `ACCOUNT_LOGIN_ON_PASSWORD_RESET` is False, so `Auth` reads 401 without errors or pending flow as success. An accepted request writes `password_reset_requested` (user id for a known account, none otherwise, the address as keyed `subject`).

## Tests

- `tests/test_auth_lifecycle.py`: generic response, session binding, single use, revocation, 15-character minimum, expiry, attempt limit, unsafe identity states, ownership proof, stage ended by sign-out. `tests/test_session_serialization.py`: parallel wrong and correct codes, missing or expired session, non-POST methods, CSRF denial. `tests/test_session_lifecycle.py`: sessions end on reset. `tests/test_pending_process_authority.py` and `tests/test_stale_account_processes.py`: code ended by a credential change, failed completions answer 409. `tests/test_code_mail_session.py`: the code is queued with its session. `tests/test_company_registration.py`: recovery after the signup race. `tests/test_transactional_mail.py`, `tests/test_account_mail_content.py`: code mail, unknown-address and completion notices. `tests/test_account_mail_failures.py`: with a failing mailer the request answers alike for known and unknown addresses and the reset completes. Playwright: `web/tests/auth-registration.spec.ts` (mocked happy path, pending-flow precedence, stage exit), `web/integration/auth-security.spec.ts` (real stack reset, then TOTP login).
- Gaps: no test of the 429 limits, `login_failed` clearing, an address verified elsewhere before the reset, removal of provider identities, or any frontend error state.
