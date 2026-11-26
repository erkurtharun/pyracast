# Account mails: codes, security notifications and anti-enumeration notices

Every mail django-allauth sends about an account goes through one adapter: verification, reset and reauthentication codes, security notifications and the notices that replace a code when the address is unknown or already registered. Other flows trigger it from their headless requests. The adapter stores an `OutboundMail` intent in the request's transaction (kind, language, allowlisted string context, the code sealed); the notification worker renders the templates and sends it through the default mailer after the commit. The request never waits for or reveals delivery; an uncertain send is never repeated (flow `email-transport`). Actors: any account holder or anonymous requester; the operator command `reset_mfa`; no other command or script.

## Entry points

- `pyracast.accounts.adapters.AccountAdapter`: `send_mail` (language, intent), `send_notification_mail` and `notify` (add the `user` to the context), `render_mail` (worker side). Wired by `ACCOUNT_ADAPTER`; behaviour gated by `ACCOUNT_EMAIL_NOTIFICATIONS`, `ACCOUNT_PREVENT_ENUMERATION` and the `*_BY_CODE_*` settings in `src/pyracast/settings.py`.
- `pyracast.accounts.mail_intents` `enqueue_mail`, `seal_code`, `enqueue_lock_notice`; the worker calls `pyracast.accounts.account_mail.compose_account_mail` from `pyracast.notifications.outbox.send_outbound_mail`.
- Code mails, project templates on `src/pyracast/accounts/templates/mail/base.html` plus `src/pyracast/accounts/templates/mail/code.html`: `email_confirmation{,_signup}` for signup, unverified sign-in, `auth/email/verify/resend` and `account/email`; `password_reset_code` for `auth/password/request`; `login_code` for `auth/reauthenticate/code/request` (`web/src/features/account/security-reauthentication.tsx`), open only to an account with neither password nor authenticator.
- Notices, `pyracast.accounts.account_mail.NOTICES` rendered by `render_notice` into `src/pyracast/accounts/templates/mail/notice.{txt,html}`: security notices for password, email, two-factor, recovery-code and sign-in provider changes; `second_factor_reset` after the operator command `reset_mfa`; anti-enumeration notices `unknown_account` (reset request for an address without an eligible account) and `account_already_exists` (password or provider signup with a registered address); `login_locked` when failed sign-ins pause password sign-in. Triggers: the password, email, TOTP, recovery-code, provider-link, reset and signup endpoints (flows `password-change`, `password-reset`, `account-email-change`, `two-factor-management`, `linked-accounts`).

## Sequence

```mermaid
sequenceDiagram
    participant F as Allauth flow
    participant A as AccountAdapter
    participant DB as PostgreSQL
    participant W as Mail worker
    participant T as Templates
    participant M as Mail backend
    alt security notification
        F->>A: "send_notification_mail(prefix, user)"
        A->>A: "skip without EMAIL_NOTIFICATIONS or any address, else send_mail with user"
    else code or notice
        F->>A: "send_mail(prefix, email, context)"
    end
    opt context holds a User
        A->>DB: "read UserLanguagePreference"
    end
    A->>DB: "insert OutboundMail in the request's transaction: kind, language, allowlisted context, sealed code, expiry"
    alt transaction rolls back
        A-->>F: "no intent, no mail"
    else commit
        W->>DB: "claim a due intent with a lease; without a context user read the address owner's language"
        W->>T: "compose_account_mail under translation.override: notice or code template"
        T-->>W: "subject, text and HTML, header X-Pyracast-Mail"
        W->>M: "send(message)"
        W->>DB: "record sent, retry, failed, uncertain or skipped; erase the sealed code"
    end
```

## Data

| Table | R/W | What |
|---|---|---|
| `notifications_outboundmail` | W (request), R/W (worker) | One intent per mail: `kind`, `recipient`, `language`, `owner_language`, `context` (strings only), `sealed_code`, `expires_at`, status and lease. The retention phase deletes ended intents 7 days after `finished_at`, at most 500 per dispatcher cycle, oldest first; account erasure deletes the intents whose `recipient`, `from_email`, `to_email` or `deleted_email` equals an address of the erased account, compared whole and case-insensitively (flow `account-erasure`). The table also holds invitation and workspace event mail (flow `email-transport`) |
| `accounts_userlanguagepreference` | R | Saved language of the recipient. Written by signup, `/api/session/language/` and the admin language switch. |
| `account_emailaddress` | R | Notification recipient (primary address, else `auth_user.email`; `email_changed` names the old address); the worker finds the account of a verified address when the context names no user. |
| `django_session` | none here | Code, attempts and `resend_count` are written by the triggering flow before the mail is stored. |
| external `MAILERS["default"]` | W | SMTP or Gmail API (console only with `PYRACAST_DEBUG=1`) (flow `email-transport`). |

## Authorization

- The adapter authenticates nothing. The triggering view and allauth flow enforce CSRF, session, reauthentication and rate limits: project `reset_password` 5/h/ip and 3/h/key, `confirm_email` 1/1m/key and 6/h/key; allauth default `reauthenticate` 10/m/user for the reauthentication code.
- The recipient is chosen by the flow: the primary address for notifications, the old address for `email_changed`, the typed address for verification codes and notices, and for reauthentication codes one of the user's own verified addresses (allauth input validation).
- Anti-enumeration is allauth policy: unknown or registered addresses get a notice mail and the same HTTP answer as a real account.
- Deployment check `accounts.E001` refuses production mail that cannot deliver (flow `email-transport`).

## Failure paths

| Condition | Result |
|---|---|
| Transaction rolls back | No intent, no mail |
| `EMAIL_NOTIFICATIONS` off, or no primary address and no `user.email` | Notification silently skipped |
| Verification resend within 1 minute of the previous mail, or a fourth resend or no pending code | 429, or 409; the UI asks the user to wait a minute (429) or to sign in again for a new code (409) and disables the button for 60 s after each send |
| Client IP unparsable when a notification is built | `PermissionDenied`, 403, after the account change is saved; no mail |
| Known non-send (SMTP or Gmail) | Retried with backoff until the intent expires; `outbound_mail_retry` warning |
| Permanent refusal, retries exhausted, or an uncertain send | Ended `failed` or `uncertain` with one ERROR line `outbound_mail_failed` (no address, code or exception text); an uncertain code mail is never sent again, so the person asks for a new code |
| Verification code asked again after a failed or uncertain mail | Allowed once the one-minute `confirm_email` window has passed |
| Expired intent, address without a sign-in account (`login_locked`), or the anonymous budget spent | Ended `skipped` (`outbound_mail_skipped`), the code erased; no mail |
| Mail failure in password change or set | The change, logout, signal and audit event are as on success |
| Unknown or registered address | Same response as success; the notice mail is stored, and two such kinds share 20 transmissions an hour in the worker (`reason=budget`) |

## Rules and invariants

- Language is fixed in the stored intent: the saved preference of the context user, else the request language; without a context user (`owner_language`) the worker prefers the language of the account owning the address.
- Every account mail carries `X-Pyracast-Mail: <template prefix>`; subjects have no prefix.
- One HTML frame, `src/pyracast/accounts/templates/mail/base.html`, serves the HTML part of account, invitation, event and forecast mail (the forecast failure notice is plain text only and has no frame): the lower-case wordmark, light and dark colour schemes (a `prefers-color-scheme: dark` block), format-detection and reformatting metas, `dir` and a `role="article"` wrapper labelled by a `label` block each mail sets. No mail carries hidden text: its first visible lines are the preview. `src/pyracast/accounts/templates/mail/action_link.html` (a button plus the printed address) serves the account mails and the workspace invitation mail; `src/pyracast/accounts/templates/mail/text_link.html` is the underlined text link of the forecast and workspace event mails.
- Workspace event mails (`notifications/event_messages.py`): the decision mails (approved, not approved, suspended) and the purge warning list Plant, Workspace and the plant's time of the event with its offset; the role mails name the workspace. Their footer reads "Email about the workspace {workspace}, sent to the people with a role in it.".
- The context keeps only allowlisted string keys (`MAIL_CONTEXT_KEYS`); a code is stored only sealed (`seal_code`), only in code kinds and only while the intent is open, and is erased on every end.
- An intent lives as long as its code (verification and reset 600 s, reauthentication `ACCOUNT_LOGIN_BY_CODE_TIMEOUT` 180 s), a notice one day.
- Code mails state the expiry in whole minutes; verification and reset run by code, with no link mode.
- Security notices show the time in Türkiye time with its offset (`15 Nov 2026, 15:00 (UTC+3)`, `account_mail.ACCOUNT_ZONE`), a device label (browser and system, never the raw user agent), the IP address and a link to secure the account on `PYRACAST_PUBLIC_ORIGIN/app/`. Notices that are not security changes (`unknown_account`, `account_already_exists`, `login_locked`, `second_factor_reset`) carry neither device nor IP. A notice's sentence appears only where it adds to the heading, and the text part starts with the heading.
- In password change and set, allauth logs the session out, sends the notification and only then emits the `password_changed` or `password_set` signal (audit event). Reset emits its signal before the mail.

## Tests

- `tests/test_account_mail_content.py`: a matrix over endpoints and languages through the real headless views.
- `tests/test_account_mail_outbox.py`, `tests/test_account_mail_commits.py`.
- `tests/test_account_mail_failures.py`, `tests/test_mail_outcomes.py`: failing mailers and their log lines without address, code or traceback.
- Gaps: browser tests mock the API; no real inbox.
