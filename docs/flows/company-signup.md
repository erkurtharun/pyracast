# Company signup and email verification

An anonymous visitor creates a password account and, in the same request, a new workspace (tenant) of which they become the only administrator. Mandatory email ownership follows: a mailed code entered in the same browser session verifies the address and signs the user in. With an invitation token the form creates only the user; the membership comes from `invitation-acceptance`.

## Entry points

- Screen: `web/src/features/auth/auth.tsx` `Auth`, modes `register` then `verify`, opened by `RequireUser` for `/app/?signup=1` (links in `public-site/home-page.tsx`, which also pass `language` for `POST /api/session/language/`, and the allauth `unknown_account` mail). "Use a different address" ends the pending sign-up through `DELETE auth/session`.
- `POST /api/auth/browser/v1/auth/signup` -> `pyracast.accounts.signup.CompanySignupView` (input `CompanySignupInput`), routed in `src/pyracast/accounts/auth_urls.py`, which lists every browser auth route; anonymous. `provider/signup` reuses `AccountSignupForm` and `register_account` (flow `social-sign-in`).
- `POST .../auth/email/verify` -> `pyracast.accounts.headless.VerifyEmailView`; `POST .../auth/email/verify/resend` -> `ResendEmailVerificationCodeView` (both in `accounts/auth_urls.py`).
- The mail is an outbound intent sent by the mailer actor `send_outbound_mail` (`account-mail-notifications`, `email-transport`).

## Sequence

```mermaid
sequenceDiagram
    participant UI as Auth screen
    participant V as CompanySignupView
    participant A as allauth flows
    participant DB as PostgreSQL
    participant W as Mailer worker
    UI->>V: POST auth/signup with company, email, password, X-CSRFToken
    V->>DB: one transaction from before validation: signup rate-limit counter, then email and invitation reads
    alt rate limited, session busy, invalid input or bad invitation
        V-->>UI: 429, or 400 field errors
    else email already registered
        V->>A: prevent_enumeration, no user created
        A->>DB: account_already_exists intent
        V-->>UI: 401 verify_email pending, same as success
    else new email
        V->>DB: user, address, tenant, admin membership, language preference
        V->>A: complete_signup
        A->>DB: code intent, and code, attempts and login stage saved in the session
        V-->>UI: 401 verify_email pending, never before the minimum answer time
    end
    W->>DB: after the commit, send the pending intent
    UI->>A: POST auth/email/verify with the code
    A->>DB: on a matching code, lock the account rows, mark address verified, log in
    A-->>UI: 200 signed in, 400 wrong code or email_taken, 409 no pending code or account changed
    UI->>A: optional POST auth/email/verify/resend
    A->>DB: new code intent and session saved in one transaction, unless throttled or capped
    A-->>UI: 200, 409 or 429
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `auth_user` | R/W | New non-staff user; rows locked during creation and confirmation; `last_login` set on verification |
| `account_emailaddress` | R/W | Primary address inserted unverified, then verified; reads decide `account_already_exists` |
| `accounts_tenant`, `accounts_membership` | W | Tenant and `admin` membership via `register_account`; none with an invitation token |
| `accounts_userlanguagepreference` | W | The request language, always; read when mailing |
| `accounts_workspaceinvitation` | R | Token, email, unused and unexpired checked; not consumed here |
| `notifications_outboundmail` | W | Code intent (`email_confirmation_signup`, sealed code, expiry 10 minutes) or `account_already_exists` intent |
| `django_session`, `usersessions_usersession`, `pyracast_authentication_cache` | R/W | Code, attempts, resend count, login stage; user session row at the verification login; `signup` and `confirm_email` counters on the `auth_cache` connection |

## Authorization

- Anonymous by design; CSRF is enforced by `CsrfViewMiddleware` before the view; a signed-in session with valid input gets 409.
- Rate limits in `ACCOUNT_RATE_LIMITS` (`pyracast/settings.py`): `signup` 5/h/ip, consumed before validation; `confirm_email` 1/1m/key and 6/h/key (the email) on every code or anti-enumeration mail. `AccountAdapter.get_client_ip` refuses requests without a determinable IP (403).
- `SessionRequestSerialization` takes an advisory lock per session cookie. The database guards `membership_tenant_administrator_uniq` and allauth's `unique_verified_email`. Sign-in with an unverified address re-enters the code stage.

## Failure paths

| Condition | Result |
| --- | --- |
| Missing CSRF token / already signed in / body not a JSON object | 403 `csrf_failed` / 409 / 400 `bad_request` |
| Sixth signup attempt in an hour from one IP, or session lock held over 5 s | 429, or 429 `session_busy` with `retry_after` |
| Bad email, or password under 15 characters, common, over 1024 or similar to the address | 400 `email` or `password` |
| Workspace name blank, over 200, or holding control characters or web or email address text | 400 `company_name`, nothing committed; a name may repeat |
| Invitation token unknown, expired, used or for another email | 400 non-field "The invitation is invalid or expired." |
| Email already registered | 401 `verify_email`, identical to success; the mail is `account_already_exists` |
| Verification mail allowance of the address spent (1 per minute, 6 per hour), either kind of address | 401 `verify_email` as on success, no mail; only the `signup` limit answers 429 |
| Mail cannot be delivered | The request is unaffected: 401 `verify_email` as on success, the intent is retried or ends `failed` |
| Wrong code (third wrong attempt discards the code) / address verified for another account meanwhile | 400 `key` / 400 `email_taken` |
| No pending code in this session, 10 minutes after signup, attempts used, or the new account changed meanwhile | 409; the pending verification is discarded |
| Resend within 1 minute of the last mail / after 3 resends / no pending code or account changed | 429 / 409 / 409, no mail |
| Resend whose session row cannot be locked in time / ended meanwhile | 503 `busy` / 400 `session_ended`, no code queued |

## Rules and invariants

- `starting` opens the request's one transaction before the input is validated; user, address, tenant, membership and language commit together, the code intent is stored in it and the session holding the code is saved before the commit, so a failed save queues no mail. A failure after the user insert rolls everything back.
- Every `POST auth/signup` not refused by a rate limit answers no sooner than `PYRACAST_SIGNUP_MINIMUM_ANSWER_SECONDS` (1 s) after it reaches the middleware `SignupMinimumAnswerTime`. The wait runs after the commit; a 429 and other methods answer at once. An address with an account hashes the password like a new account.
- Code verification logs the user in when the pending stage belongs to the same user; the pending code and the sign-in step rest on the new account's credential state (`accounts/processes.py`). The process lasts 10 minutes from signup and is not extended by a resend; 3 wrong attempts in total; 3 resends, each replacing the code.
- A race can leave two unverified accounts for one mailbox; the first to prove the mailbox by password reset owns it. An active signup that never signed in and proved no address is removed with its own empty workspace seven days after `date_joined` by the dispatcher's retention phase (`accounts.retention.reclaim_abandoned_signups`).

## Tests

- `tests/test_company_registration.py`: end to end signup, verification and tenant access, duplicate email, rollback, attempt limit, minimum answer time.
- `tests/test_session_serialization.py` (resend throttle and cap), `tests/test_stale_account_processes.py` (address verified meanwhile), `tests/test_account_retention.py` (abandoned signups).
- `web/tests/auth-registration.spec.ts` and `web/tests/workspace-roles.spec.ts` mock the API; no browser test reaches Django. Untested: the password endpoint's 5/h/ip limit through the real middleware and the `email_taken` answer.
