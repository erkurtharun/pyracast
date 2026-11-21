# Invite a member by email, list, resend and cancel invitations

A workspace administrator submits an email address on the Members screen. The backend stores a seven-day, single-use invitation (superseding any earlier open one for that address) and a mail intent in one transaction. The mailer then mints the token and sends the link `{PYRACAST_PUBLIC_ORIGIN}/app/invitation/#token=...`. The administrator can list open invitations, send one again and cancel one; acceptance is the separate `invitation-acceptance` flow.

## Entry points

- `web/src/features/workspace/workspace-members.tsx` `WorkspaceMembers` at `/app/w/<tenant>/members/` (`MembersPage`, administrators only) composes `members/invite-member.tsx` `InviteMember` (form and the `plants_at_recipient_limit` warning) and `members/pending-invitations.tsx` `PendingInvitations` (state, role, sent and expiry dates, the delivery hints `deliveryFailed` and `deliveryUncertain`, "Send again", "Cancel invitation"; one action at a time).
- `GET|POST /api/tenants/{t}/invitations/` -> `pyracast.api.workspaces.WorkspaceInvitationsView`; `DELETE .../invitations/{i}/` -> `WorkspaceInvitationView`; `POST .../invitations/{i}/resend/` -> `WorkspaceInvitationResendView` (`src/pyracast/urls.py`). Actor: workspace administrator. They call `pyracast.accounts.invitations.invite_workspace_member`, `reissue_workspace_invitation` and `revoke_workspace_invitation`, as does the platform console with `platform=True`: `/admin/workspaces/{id}/invitations/new/`, `.../invitations/{i}/reissue/` and `.../invitations/{i}/revocation/` (`pyracast.platform_admin.tenant_views`), and workspace creation, which can invite the first administrator.
- Mail templates `src/pyracast/accounts/templates/mail/workspace_invitation.txt` and `.html`. No command or script. The intent is an `OutboundMail` row; the dispatcher publishes due intents to the `send_outbound_mail` actor (`pyracast.notifications.outbox`), which sends through `pyracast.mail.send.send` (flow `email-transport`).

## Sequence

```mermaid
sequenceDiagram
    actor A as Administrator
    participant UI as WorkspaceMembers
    participant V as Invitation views
    participant Op as Invitation operations
    participant DB as PostgreSQL
    participant W as Mailer
    participant M as Mail backend
    A->>UI: "submit email"
    UI->>V: "POST invitations/ with X-CSRFToken"
    V->>V: "session auth, CSRF, route policy admin, InvitationInput"
    alt "not authenticated, not admin, unknown field or bad email"
        V-->>UI: "401, 403 or 400"
    end
    V->>Op: "invite_workspace_member(actor, tenant_id, email)"
    Op->>DB: "BEGIN, lock tenant row, check admin, member check"
    Op->>DB: "sender and address advisory locks, count rows per budget"
    alt "existing member or budget used up"
        Op-->>V: "DomainError, rollback"
        V-->>UI: "400, or 429 with Retry-After"
    end
    Op->>DB: "supersede open rows for the address, insert invitation and mail intent, COMMIT"
    V-->>UI: "201 id, email, role, state, created_at, expires_at, delivery_status pending"
    W->>DB: "lock the intent and the open invitation, store the digest of a new token"
    W->>M: "send(message with the link)"
    W->>DB: "delivery_status = sent, failed or uncertain"
    UI->>V: "GET invitations/ (refetch after each action)"
    V->>DB: "open invitations, newest first"
    V-->>UI: "200 invitations"
    A->>UI: "Send again or Cancel invitation"
    UI->>V: "POST invitations/{i}/resend/ or DELETE invitations/{i}/"
    V->>Op: "reissue_workspace_invitation or revoke_workspace_invitation"
    Op->>DB: "lock tenant, check admin; resend invites the same address again, cancel locks the row and stores revoked_at"
    V-->>UI: "201 invitation, or 204, then refetch"
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `accounts_workspaceinvitation` | R/W | Insert with `role`, `created_by`, `expires_at = now + 7 days`, `delivery_status` `pending` and no token; the mailer stores the SHA-256 `token_hash` (unique) and the delivery status; supersede and cancel store `revoked_at` and `revoked_reason` and never move `expires_at`; the list reads open rows; budgets count rows by `created_at` |
| `notifications_outboundmail` | W | One `workspace_invitation` intent per invitation, written in the same transaction; it never holds the token |
| `accounts_tenant` | R (`FOR UPDATE`) | Tenant lock in `tenant_membership(lock=True)`; workspace name for the mail |
| `accounts_membership`, `auth_user`, `accounts_userlanguagepreference` | R | Administrator check; existing-member check through `user__email`; administrator's mail language |
| `django_session`, `usersessions_usersession` | R/W | Session middleware on every request under a per-session advisory lock. Limits are database counts, not the authentication cache |
| `notifications_reminderschedule` and related | R | `plants_at_recipient_limit` of the members read, shown before an invitation that would pause reminders |

## Authorization

- Authentication and CSRF: `SessionAuthentication` plus `RoutePolicy` (`REST_FRAMEWORK` in `settings.py`); CSRF is enforced on an authenticated `POST` and `DELETE`, and the SPA sends `X-CSRFToken` from the session payload.
- Route: every invitation route is `ADMIN` in `ROUTE_POLICIES` (`pyracast.api.permissions`): an active membership of `tenant_id` without a lock, then the administrator role. The list read takes no tenant lock.
- Operation: `authorize_workspace_admin` locks the tenant row through `tenant_membership(role=admin, lock=True)` and re-reads the membership; with `platform=True` a platform administrator replaces the membership check. An `admin` invitation needs platform authority (403 `platform_admin_required` otherwise) and a workspace without administrator (409 `workspace_admin_changed`); the API only ever invites viewers.
- Database: `token_hash` unique; `workspaceinvitation_tenant_email_uniq` (one open invitation per workspace and address), `workspaceinvitation_tenant_administrator_uniq` (one open administrator invitation per workspace), `workspaceinvitation_revoked_at_chk`, `workspaceinvitation_accepted_by_chk` and checks on `delivery_status`, `role` and `revoked_reason`. The Members screen is admin-only in the UI as a convenience.

## Failure paths

| Condition | Result |
| --- | --- |
| Not authenticated | 401 `not_authenticated` with `WWW-Authenticate: Session` |
| Missing or wrong CSRF token (`POST`, `DELETE`) | 403 `csrf_failed` |
| Foreign tenant, any method | 403 `membership_required` |
| Viewer, any method | 403 `workspace_admin_required` |
| Resend of an administrator invitation through the API | 403 `platform_admin_required` |
| Unknown JSON field such as `role`, invalid email, over 254 characters | 400 `invalid_request` with "Unknown fields are not accepted." or a field error |
| Email already a member (`user.email`, case-insensitive, inactive users included) | 400 `invitation_member_exists` |
| A budget is used up (see Rules) | 429 `invitation_rate_limited`, `Retry-After` and `retry_after` equal to the wait of the largest violated budget |
| Tenant or invitation row lock not acquired within 5 s; same-session request still running after 5 s | 503 `busy` / 429 `session_busy`, both `Retry-After: 1` |
| Cancel or resend with an unknown id, an id of another tenant or an accepted invitation; resend of a closed one | 400 `invitation_unavailable` |
| Cancel of an invitation already cancelled or superseded | 204, nothing changes |
| Mail not sent or not confirmed | Still 201; the worker ends the intent with `failed` or `uncertain` and the list shows the hint |

## Rules and invariants

- Lock order: tenant row, sender and address advisory locks (`INVITATION_LOCK_SPACE`), then the invitation row (cancel). The tenant lock serializes issuance per workspace; the advisory locks serialize the budgets that span workspaces.
- Budgets (`InvitationBudget` in `invitations.py`): 20 an hour per workspace; one a minute per workspace and address (not counting an invitation whose delivery ended `failed` or `uncertain`, so it can be sent again at once); 50 a day per sender; 3 a day per address; one workspace per address a day. Cancelled, superseded and accepted rows count. Refusals are logged as `invitation_limited` with a redacted address.
- One open invitation per address: a new invitation sets `revoked_at` and `revoked_reason` `superseded` on every open invitation of that address in the workspace, expired ones included; an administrator invitation also closes every open administrator invitation. `WorkspaceInvitation.state_at` derives `pending`, `accepted`, `revoked`, `expired` or `superseded`; `expires_at` is the natural deadline and never moves. Cancel stores reason `cancelled`; member removal and administrator reassignment close with `member_removed` and `administrator_changed`.
- Resend takes an open invitation of this workspace (pending or expired) and issues a new one with the same address and role under the same budgets; the old one becomes superseded.
- No token exists until the mailer sends: it mints one, stores only its digest and renders the link into the message. An invitation closed before the send is skipped (`invitation_unavailable`) and stays `pending`; a send that fails before transmission is retried with a new token. The HTTP answer always carries `delivery_status` `pending`.
- Mail language is the language chosen in the console form, else the administrator's saved language, else the request language, never the invitee's. The mail names the offered role and says "seven days". No code path of this flow deletes rows.

## Tests

- `tests/test_workspaces.py` (API scoping, secrets never returned, mail language, hourly limit), `tests/test_invitation_lifecycle.py` (state, cancel, supersede, send again, listing), `tests/test_invitation_limits.py` (each budget, wait values, race between workspaces, address length, log lines), `tests/test_workspace_admin_invitations.py` (administrator invitations), `tests/test_workspace_ledger_constraints.py` (database constraints), `tests/test_workspace_http_failures.py` (status, code, message and `Retry-After` of every failure above), `tests/test_workspace_authorization.py` (viewer 403; lists answer while a writer holds the tenant).
- Mail: `tests/test_outbound_mail.py` (answer before send, token only in the message, retries, uncertain and failed delivery, resend at once), `tests/test_transactional_mail.py` (content, escaping, `#token=` link). Console: `tests/test_admin_workspace_management.py`. The members read's `plants_at_recipient_limit`: `tests/test_reminder_boundary.py`.
- `web/tests/workspace-roles.spec.ts` (invite, send again, cancel against a mocked API), `web/tests/workspace-invitations.spec.ts` (row buttons and one action at a time).
- Gaps: no real-backend Playwright run; no Playwright test shows the recipient-limit warning (mocks return 0).
