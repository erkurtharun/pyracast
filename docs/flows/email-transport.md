# Email transport: SMTP and Gmail backends

Every outgoing mail is handed to one entry point, `pyracast.mail.send.send`, which opens the mailer `MAILERS["default"]` selected by `PYRACAST_EMAIL_BACKEND` at settings import. Only the notifications worker sends: account mail, workspace invitations and workspace event mail are stored as `OutboundMail` intents in the requesting transaction and sent by `pyracast.notifications.outbox`; reminders go through `pyracast.notifications.jobs`. The outcome is typed: accepted, `NotSentError` (known not sent, retryable), `PermanentlyNotSentError`, or `DeliveryUncertainError`, each with one `MailFailure` code. Reminders persist the code; an outbound mail ends `sent`, `failed`, `uncertain` or `skipped`. Nothing verifies delivery after the fact.

## Entry points

- Settings `src/pyracast/settings.py` (`_email_backend`, `MAILERS`, `DEFAULT_FROM_EMAIL`): `smtp` (default), `gmail`, or `console` (needs `PYRACAST_DEBUG=1`, otherwise `ImproperlyConfigured` at import). `PYRACAST_EMAIL_SSL=1` alone selects implicit TLS. The Gmail sender is `PYRACAST_FROM_EMAIL`.
- Intent writers (`pyracast.accounts.mail_intents`): `enqueue_mail` from `AccountAdapter.send_mail` (flow `account-mail-notifications`), `accounts.invitations.invite_workspace_member` (reached by `POST /api/tenants/{tenant_id}/invitations/`), `accounts.event_mail` and `purge_warning` (workspace event mail); `enqueue_lock_notice`.
- Outbound mail worker: the dispatcher's notifications phase (`notifications.runtime.dispatch_notifications`) runs `publish_outbound_mail`, which ends expired intents and hands each due one to the actor `orchestration.kinds.OUTBOUND_MAIL` (queue `notifications`, `max_retries=0`); `deliver_outbound_mail` calls `outbox.send_outbound_mail`.
- Reminder mail: actor `orchestration.kinds.REMINDER` (`send_forecast_reminder`) to `deliver_notification` to `process_notification` to `send(message, before_send=...)`.
- Command `pyracast check_mail`: without arguments opens and closes the transport without sending; `--send-to ADDRESS` sends one reminder-shaped test mail. Exit code per `MailFailure` (`docs/DEPLOYMENT.md`). `deploy/install-release.sh` runs the probe after a healthy release.
- Deploy check `pyracast.accounts.checks.check_authentication_mail_delivery` (`accounts.E001`), run by `check --deploy` in `deploy/install-release.sh` and `deploy/smoke.sh`. Outcomes are shown in `web/src/features/workspace/members/pending-invitations.tsx` (invitation failed or uncertain hint) and on the admin Operations page ("Email delivery" table).

## Sequence

```mermaid
sequenceDiagram
    participant R as API request
    participant D as Database
    participant W as Mail worker
    participant M as send
    participant B as Backend
    participant X as SMTP or Gmail
    R->>D: "store OutboundMail intent in the request's transaction"
    W->>D: "claim a due intent: lease, attempts + 1"
    W->>M: "send(message, before_send)"
    M->>B: "MAILERS default builds a new backend instance"
    alt configuration invalid
        M-->>W: "NotSentError mail_configuration_invalid"
    end
    B->>X: "open: connect, STARTTLS, login if configured / OAuth token refresh"
    alt open fails
        B-->>W: "NotSentError: temporary non-send or credentials rejected / PermanentlyNotSent: authorization revoked"
    else before_send raises or returns False
        M->>D: "before_send: lock row, recheck lease and expiry, render message, set transmit_started_at"
        M-->>W: "NotSentError mail_preparation_failed / SendCancelledError, nothing sent"
    else open succeeds and before_send set transmit_started_at
        B->>X: "SMTP: EHLO, MAIL with BODY=8BITMIME if offered, every RCPT, then DATA / Gmail: one POST with raw MIME"
        alt known refusal
            X-->>B: "4xx or Gmail 401, 429, quota 403 = NotSent / 5xx or other Gmail 4xx = PermanentlyNotSent"
        else lost or unconfirmed reply after DATA began
            X--xB: "timeout, disconnect, Gmail 408 or 5xx, no message id = DeliveryUncertain"
        end
        B-->>W: "return or typed error"
    end
    W->>D: "record sent, retry, failed or uncertain; erase the sealed code; invitation delivery_status"
```

## Data

| Table | R/W | What |
|---|---|---|
| `notifications_outboundmail` | R/W | Written by the request (`pending`); the worker leases it, sets `attempts`, `transmit_started_at`, `status`, `last_error`, `finished_at` and erases `sealed_code` on every end. Ended intents are deleted 7 days after `finished_at` |
| `accounts_workspaceinvitation` | W | `delivery_status` set to `sent`, `failed` or `uncertain` when the intent ends; the token digest is minted at send time |
| `notifications_notificationdelivery` | R/W | Reminder only: `recipients`, `message_token` (stable `Message-ID`), `transmit_started_at`, `status`, `last_error`, `sent_at`, `attempts` (owned by `reminder-delivery`) |
| `accounts_tenant`, `notifications_notificationsettings`, `regions_region` | R (row lock) | `_mark_transmit` locks them before writing a reminder's transmit marker |

## Authorization

- The transport has no caller check: it sends whatever the caller built. Authorization is the caller's: `authorize_workspace_admin` in `invite_workspace_member`, allauth flows for account mail, and at send time the intent's own recheck (an invitation must still be pending, event mail needs a current member) and, for reminders, the tenant and region lock plus recipient recheck in `_mark_transmit`.
- Gmail enforces one sender: `_send` refuses a message whose `From` differs from the `DEFAULT_FROM_EMAIL` address and the token response must carry exactly the `gmail.send` scope.

## Failure paths

| Condition | Result |
|---|---|
| Unknown backend name, `console` without `PYRACAST_DEBUG=1`, or a non-integer port | `ImproperlyConfigured` at settings import, process does not start |
| Production check: no SMTP or Gmail backend, sender not a valid mailbox or at `localhost`, SMTP host empty or loopback, SMTP port outside 1 to 65535, SMTP without TLS or SSL, or options the backend refuses (both TLS modes, invalid Gmail sender, timeout at or below 0) | `accounts.E001`, `check --deploy` fails, release stops |
| Backend construction fails at send time | `NotSentError` `mail_configuration_invalid`; retried |
| SMTP connect or TLS error / authentication 4xx / authentication 5xx | `NotSentError` `mail_not_sent` / `mail_not_sent` / `mail_credentials_rejected` |
| Gmail token endpoint unreachable or 5xx / 429 / `invalid_grant` / other token failure or wrong scope | `NotSentError` `mail_not_sent` / `NotSentError` `mail_rate_limited` / `PermanentlyNotSentError` `mail_authorization_revoked` / `NotSentError` `mail_credentials_rejected` |
| SMTP 4xx on MAIL, any RCPT or DATA; disconnect before DATA | `NotSentError`, retry; DATA is never sent after a refused recipient |
| SMTP 5xx on MAIL, all RCPT or DATA, invalid address or header, unsupported command | `PermanentlyNotSentError` `mail_rejected` |
| SMTP disconnect or timeout after DATA began, or a final reply outside 400-599 | `DeliveryUncertainError` `mail_outcome_uncertain` |
| Gmail 401 / 429 or quota 403 / other 403 / other 4xx except 408 | `mail_credentials_rejected` / `mail_rate_limited` (both retryable) / `mail_sender_forbidden` / `mail_rejected` (both permanent) |
| Gmail connect error; 408, 5xx, read or write timeout, 200 without message id | `NotSentError`; `DeliveryUncertainError` |
| Message without recipient, or backend count other than 1 | `NotSentError` |
| Outbound mail: known non-send / permanent refusal / uncertain | `retry` with backoff (at most 8 attempts, only before the intent expires, else `failed`) / `failed` at once / `uncertain`, never sent again and never reopened |
| Outbound mail: worker lost before / after the transmit marker | After the lease ends: `retry` (`interrupted_connection`) / `uncertain` (`interrupted_send`) |
| Outbound mail: expired intent, closed invitation, no account for a sign-in pause notice, reported row gone, recipient left the workspace, anonymous budget spent | `skipped` with a reason code (`expired`, `invitation_unavailable`, `no_account` and others); nothing sent |
| Outbound mail or reminder: unclassified exception before / after the transmit marker | Retry with `mail_not_sent` / `uncertain` with `mail_outcome_uncertain`; logged as `outbound_mail_send_failed` or `notification_send_failed` |
| Invitation intent ends `failed` or `uncertain` | `delivery_status` follows; the API answered 201 when it was stored |

## Rules and invariants

- `send` builds a fresh backend for every message, so no connection or OAuth token is shared between threads or messages; Gmail refreshes a token for every mail.
- Wire bytes: CRLF line ends; `Content-Type` is never folded into encoded-words; non-ASCII subjects use encoded-words of at most 75 characters and attachment names keep RFC 2231. SMTP sends an 8-bit body only to a server that advertises 8BITMIME, else 7-bit transfer encodings. Reminders carry `Message-ID: <reminder.{message_token}@{sender domain}>`.
- Open-before-authorize: the connection is opened outside any database transaction, then `before_send` rechecks authority under a row lock, renders the message and writes `transmit_started_at`. After that marker an interruption is `uncertain` and never resent; an outbound mail is therefore sent at most once.
- The mailer claims an intent with a lease and checks the lease and expiry again after the row lock; expired or taken-over leases record nothing.
- Reminder recipients travel as BCC: SMTP puts them in the envelope only, Gmail adds a `Bcc` header because it routes by headers.
- Retry budget for known non-sends (outbound mail and reminders): at most 8 attempts, delay `30 * 2^attempts` seconds capped at 1800 plus 0-30 s jitter; a reminder retries only while the retry time is before `scheduled_for + 2 hours`, an outbound mail while it is before `expires_at`.
- All timeouts are 15 s (SMTP socket, httpx per operation). Requests never wait for a transport.

## Tests

- `tests/test_mail_wire.py` (bytes on both transports), `tests/test_mail_outcomes.py` (outcome per error class and SMTP phase), `tests/test_mail_configuration.py` (deploy check, `check_mail` exit codes), `tests/test_outbound_mail.py` (lease, retry bound, crash and takeover, uncertain, expiry), `tests/test_notification_transmission.py`.
- Gaps: no live Gmail or provider SMTP; no Playwright test touches mail (browser tests mock the API).
