# Erase an account by anonymization

A host operator handles an erasure request by running `pyracast erase_account --account ID`. Without `--execute` the command is a dry run: it does the whole erasure in a transaction that is rolled back and prints what would change. With `--execute` it anonymizes the account in place: credentials, second factors, sessions, provider identities, addresses, memberships and preferences go, as do the invitations, mail, authentication cache entries, pending flows and reminder recipient entries that name its addresses. History that points at the account (audit events, plant events, ledgers, change requests) keeps pointing at the anonymized row. No HTTP endpoint or screen starts it; the privacy notice (`web/src/features/public-site/legal-copy.ts`) tells people to request it by mail.

## Entry points

- `pyracast erase_account --account ID [--execute]`: `pyracast.accounts.management.commands.erase_account` `Command.handle`, through the `pyracast` script (`pyracast.cli` `main`). Actor: operator in the `api` container (`docs/DEPLOYMENT.md`, which also asks for a backup before `--execute`).
- Operation: `pyracast.erasure.erase_account` -> `_erase`; the result is an `Erasure` (changed rows by model label, `already_erased`, `platform_administrator`, `staff`, `addresses_left_out`). `pyracast.erasure.is_erased` and `anonymized` define an erased account.
- Retention of what erasure leaves: the sweeps of `pyracast.retention.RETENTION` (flow `dispatcher-cycle`).

## Sequence

```mermaid
sequenceDiagram
    actor O as Operator
    participant C as erase_account
    participant D as PostgreSQL
    participant L as Security log
    O->>C: "--account ID, optional --execute"
    C->>D: "read the account; already erased: report, stop"
    C->>D: "refuse if it administers a workspace (no lock yet)"
    C->>D: "BEGIN, OPERATOR_LOCK, lock_account"
    C->>D: "re-read: erased, erased-<id> taken, last platform administrator"
    C->>D: "collect the address set, leave out addresses held verified elsewhere"
    C->>D: "lock workspaces, refuse an administrator again, lock their regions"
    C->>D: "lock reminder entries and ties, hide visible schedules, delete entries, ties, delivery recipient rows; raise schedule revisions"
    C->>D: "lock and delete mail, then invitations; delete cache entries"
    C->>D: "delete sessions holding the account or its flows, then identity rows"
    C->>D: "anonymize auth_user, unusable password"
    alt dry run
        C->>D: "ROLLBACK"
        C-->>O: "would change: counts, addresses left out, nothing changed"
    else --execute
        C->>D: "COMMIT"
        C->>L: "account_erased target_id"
        C-->>O: "erased: counts, addresses left out"
    end
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `auth_user` | R/W | Locked; set to `username` `erased-<id>`, empty `email`, `first_name`, `last_name`, inactive, not staff or superuser, `last_login` null, unusable password. The row stays |
| `account_emailaddress`, `socialaccount_socialaccount`, `mfa_authenticator`, `usersessions_usersession`, `accounts_membership`, `accounts_userlanguagepreference`, `auth_user_groups`, `auth_user_user_permissions` | D | Every row of the account |
| `django_session` | D | Sessions of the account's device rows, and unexpired sessions without a device row that are signed in as the account, hold its pending process or one of its addresses, or hold a pending provider sign-up under its first and last name (in that order) that names no other account |
| `notifications_outboundmail` | D | Mail whose `recipient`, `from_email`, `to_email` or `deleted_email` equals one of the addresses, and mail of the invitations below |
| `accounts_workspaceinvitation` | D | Invitations addressed to one of the addresses or accepted by the account, after their mail |
| `pyracast_authentication_cache` | D | Entries whose JSON value holds one of the addresses as an exact string |
| `notifications_reminderrecipient`, `notifications_reminderrecipienttie`, `notifications_reminderschedule` | D, D, W | Entries of the addresses that no other account holds verified; ties to the account's memberships; an entry tied only to them, whatever address it holds and whoever holds that address. Every visible schedule of the membership workspaces, and every visible schedule whose entry or tie it deletes (also in another workspace), is set hidden (`hide_recipients`, `hidden_by_departure`, revision raised); the revision of each other changed schedule is raised; each schedule is raised and counted once under `notifications.ReminderSchedule` |
| `notifications_remindermemberrole` | D | The account's member choices go with its memberships (cascade) |
| `notifications_deliveryrecipient` | D | Recipient rows of past and open deliveries for the addresses; a row kept for an address another account holds verified keeps its `role` |
| `accounts_tenant`, `regions_region` | lock | Workspaces of the account's memberships and of the regions whose reminder rows it touches; after the workspaces and before any schedule, every region of those workspaces (trashed ones too) with the regions its rows lead to, in primary-key order without the key, so erasure and a purge each take a region before a schedule |

## Authorization

- No application authorization: the trust boundary is shell access to the host or container, as for `bootstrap_admin` (flow `platform-admin-bootstrap`). There is no operator identity.
- Refusals protect the platform and the workspaces: an account that administers a workspace (checked before any lock and again under the workspace locks), the last active platform administrator (checked under `OPERATOR_LOCK`, which the commands that grant the platform role also take), and an account whose `erased-<id>` username another account holds.
- An erased account cannot sign in: it is inactive, has no address, provider identity or usable password, and a later sign-up or provider sign-in with one of its addresses never reaches it.

## Failure paths

| Condition | Result |
| --- | --- |
| Unknown account id | `CommandError` "Account <id> does not exist." |
| Account already erased | "Account <id> is already erased. Nothing was changed."; exit 0 |
| Account administers a workspace | `CommandError` naming the workspaces: assign another administrator or purge each workspace first |
| Last active platform administrator | `CommandError`: create another one with `bootstrap_admin` first |
| Another account holds `erased-<id>` | `CommandError`: rename that account first |
| Lock timeout or deadlock | `CommandError` "...changed nothing. Run it again." |
| Any other database error | Raised unchanged; the transaction rolls back |

## Rules and invariants

- Address set: the account's current addresses, the earlier ones its reminder entries still tie to its memberships, and every address the outbox names as changed away from or removed (`email_changed`, `email_deleted`) for one of them, followed until no new one appears. An address another account holds verified (a verified `account_emailaddress` row; an unverified row or a bare `User.email` never counts) is left out, not followed and only counted (`addresses_left_out`), never printed.
- Comparison: addresses compare by full Unicode lower case (`str.lower`, SQL `lower(... COLLATE pg_unicode_fast)`); reminder entries and delivery recipient rows, which keep the address as typed, by their stored key: it equals `notifications.recipients.recipient_key` (the exact text with only `A`-`Z` lower-cased) of an address of the set, and an entry or row selected so stays while another account holds verified an address whose SQL `casefold(... COLLATE pg_unicode_fast)` is that key. A reminder entry is also deleted when it is tied only to the account's memberships, whatever address it holds, even one another account holds verified (`erasure._entries`); a delivery recipient row has only the address condition (`erasure._delivered`). The product stores only plain addresses, for which these case rules agree. Mail matches a whole value, never a substring.
- Reminder schedules: the hiding (`recipients.hide_visible_schedules`, in `erasure._forget_recipients` after the entries and ties are locked) records no audit event, keeps `configured_by` and `effective_from`, and leaves a hidden schedule as it is; the administrator's next save clears the mark (flow `reminder-configuration`).
- Concurrency: lock order is `OPERATOR_LOCK`, the account (`accounts.processes.lock_account`), workspaces, their regions, then reminder entries, ties and delivery recipient rows, then mail before its invitations (the mail worker's order). The statements that lock rows select by address only addresses no other account holds verified when they run, and also lock a reminder entry tied only to the account's memberships, whatever address it holds (`erasure._entries`); the deletes act on the locked ids and the deletes of mail and invitations apply that exclusion again; sessions read the held set again just before they are matched. A row another client commits after its table was locked is left, as is an invitation whose mail was queued after the mail lock.
- What a left-out address keeps: its mail, invitations, sessions and cache entries age out by their retention periods or expiry; reminder entries other memberships or administrators still hold and delivery recipient rows have no retention period and stay.
- The report lists only tables with changed rows; both runs print the count of addresses left out, and the dry run also says when the account has platform or staff access. A dry run briefly takes the same locks.
- `account_erased` (detail `target_id` only) is written when the erasure commits; a dry run writes none. Security log lines already written keep the account id, client IP and keyed address hash until their own expiry.
- The retention sweeps never select an erased account: the abandoned sign-up sweep takes only active accounts.

## Tests

- `tests/test_account_erasure.py`: every relation to an account erased or kept for a stated reason; no personal data left and history kept; dry run; exact-address mail and cache matches; earlier addresses followed; addresses held verified elsewhere kept, unverified claims ignored; tie removal; rows committed while erasure waits kept; pending sign-up matched by ordered names; one case rule beyond ASCII; only changed tables reported; administrator, last platform administrator and taken username refusals; lock conflicts change nothing; repeated and concurrent erasures; retention leaves erased accounts alone; console and API show kept history under the anonymized name; command output and refusals.
- `tests/test_reminder_addressing_departure.py`: schedules hidden, advanced and counted once, also in another workspace; erasure and a purge of a trashed region wait for each other; a save racing an erasure.
- Gaps: no test runs the command in the production container.
