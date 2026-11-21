# Rotate a provider API key or clear a provider cooldown

A platform administrator recovers a shared Solcast account in two separate actions. Saving the plant's setup with a replacement key and an explicit confirmation rotates the encrypted key and bumps `credential_revision`, which lifts a 401 block but makes every plant on the account need a new validation; with otherwise unchanged setup values the binding and approval stay. After the provider confirms recovery, a second confirmed action clears the stored request delay. Both are audited per account.

## Entry points

- Console: `POST /admin/plants/{id}/` (URL name `admin:workspace_plant`) -> `pyracast.platform_admin.plant_review.plant`; template `src/pyracast/regions/templates/admin/regions/region/setup.html`.
- Rotation: `action=configure`, `account_mode=existing`, `api_key`, `confirm_key_rotation` -> `plant_review.configure` -> `pyracast.connections.setup.save_provider_setup` -> `pyracast.connections.operations.rotate_secret` -> `pyracast.connections.secrets.encrypt_secret`, then `pyracast.regions.operations.configure_binding`; the "Replace connection" disclosure. The form is `pyracast.platform_admin.forms.ProviderSetupForm`.
- Cooldown: `action=clear_cooldown` + `provider_recovered` -> `BindingActionForm.current_binding` -> `pyracast.connections.recovery.clear_provider_cooldown`; the "Provider request delay" callout.
- No REST route rotates a key or clears a delay.

## Sequence

```mermaid
sequenceDiagram
    actor A as Platform admin
    participant V as plant_review and forms
    participant O as Operations
    participant DB as PostgreSQL
    participant W as Dispatcher and worker
    A->>V: "POST configure with api_key and confirm_key_rotation"
    V->>DB: "BEGIN, Region FOR UPDATE, check expected_revision and expected_binding_id"
    V->>DB: "enabled Solcast ProviderAccount FOR UPDATE, check key revision from the page"
    V->>O: "rotate_secret"
    O->>DB: "encrypted_api_key replaced, credential_revision + 1, audit provider_secret_rotated"
    V->>O: "configure_binding"
    O->>DB: "unchanged setup: existing SiteBinding; changed with a binding of the revision: region revision + 1 and pending, pending location request moved to the new revision (location_change_rebased), new SiteBinding, audit; first binding: new SiteBinding at the current revision, audit; COMMIT"
    alt stale revision, invalid key or binding rule
        V-->>A: "400 field or setup error, rotation rolled back"
    else saved
        V-->>A: "302 to /admin/plants/{id}/"
    end
    Note over DB,W: Other bindings of the account stop matching credential_revision and claim marks their jobs stale
    A->>V: "POST clear_cooldown with provider_recovered"
    V->>DB: "current_binding checks region revision and key revision"
    V->>O: "clear_provider_cooldown, which locks ProviderAccount FOR UPDATE"
    alt key revision or expected_cooldown_until differs
        V-->>A: "400 setup error"
    else no delay stored
        V-->>A: "302 info, There is no provider wait to clear"
    else delay matches
        O->>DB: "request_blocked_until NULL, audit provider_cooldown_cleared, COMMIT"
        V-->>A: "302 Provider wait cleared"
        W->>DB: "pending_job_ids selects provider_throttled retry jobs, claim runs current ones early"
    end
    A->>V: "POST validate per plant, one counted request, refused while a delay is active"
    A->>V: "POST approve this plant again, needed when configure reopened the revision"
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `connections_provideraccount` | R/W | Locked `FOR UPDATE`; rotation writes `encrypted_api_key` and `credential_revision`, clearing writes `request_blocked_until`; `authentication_failed_revision` and `daily_quota` are never touched |
| `connections_sitebinding` | R/W | Rotation with unchanged setup keeps the binding; with changed setup it inserts a new one; other bindings are only compared via `validated_credential_revision` |
| `regions_region` | R/W | Locked by `save_provider_setup`; `revision`, `status` and `approved_binding` change only when the setup changed and the region already has a binding for its current revision |
| `regions_regionlocationchangerequest` | W | Pending requests get `base_revision` moved to the new revision when the setup changed |
| `regions_auditevent` | W | `provider_secret_rotated` and `provider_cooldown_cleared` (account target, `revision` = `credential_revision`); `binding_configured` and `location_change_rebased` (region target) |
| `forecasts_collectionjob` | R | `claim` and `pending_job_ids` read the account delay; rotation and clearing write no job (only `validate_binding` re-arms, see `provider-connection-setup`) |

## Authorization

- Console: `PlatformAdminSite.has_permission` requires an active staff superuser and `security.has_mfa_evidence` (any age); `admin_view` adds CSRF, `never_cache`, a recent authenticator confirmation for every unsafe method and `contained`. Nothing narrower exists per action.
- Operations: `require_platform_admin` (active staff superuser) in `save_provider_setup`, `rotate_secret`, `configure_binding` and `clear_provider_cooldown`; no authenticator check there.
- Database: `provideraccount_encrypted_api_key_chk` requires the `encrypted:v1:` prefix; `auditevent_target_chk` makes account audit rows target no region. Accounts are platform-wide, shared across tenants.

## Failure paths

| Condition | Result |
| --- | --- |
| Not staff, superuser or authenticator evidence / missing CSRF | 302 to `/admin/login/`, then to `/app/` / 403 |
| Unsafe method without a recent authenticator confirmation | 403 confirmation page, nothing written |
| Key on an existing account without confirmation; clear without `provider_recovered`; account no longer offered (key revision changed or disabled; choices are rebuilt per request) | 400 with a field error ("Select a valid choice" for the account), key never re-rendered, nothing written |
| Key over 1024 characters | 400, field error |
| Key with spaces or non-printable characters | 400, "Enter an API key without spaces, up to 1024 characters.", rotation rolled back; an empty key on an existing account is no error: the binding saves without rotation |
| Stale region or binding revision, or key revision changed after the form was built (under the locks) | 400 with the specific message ("The plant changed...", "The provider setup changed...", "The account changed..."); rotation rolled back |
| Account missing or disabled when locked, or a binding rule violation | 400 "The setup could not be applied..." or the rule's message; rotation rolled back |
| Clear with a changed delay | 400 "The provider delay changed. Reload before confirming recovery.", nothing cleared |
| Clear with a changed region revision, binding or `credential_revision` | 400 generic setup message, nothing cleared |
| Delay already NULL | 302 info "There is no provider wait to clear."; nothing written, no audit row |
| Lock timeout (5 s), deadlock or statement timeout | Nothing written; retry flash and redirect to the referring console page; JSON callers get 503 |
| Session already running another request | 503 page with `Retry-After: 1`; a write is never replayed |

## Rules and invariants

- Rotation never touches `request_blocked_until` or `RequestAttempt`: a stored delay (429, or 402 until the next provider day) survives a new key and the daily budget is unchanged. The 401 block lifts only because `authentication_failed_revision` no longer equals `credential_revision` (`quota.deferral`); `record_provider_failure` ignores errors from an older revision.
- Every binding of the account stops being current (`binding_is_current` compares `validated_credential_revision`). `claim` turns their jobs `stale`, `approve_region` refuses them, `remaining_forecast_requests` skips them and customers see `freshness: "stale"`. Each plant needs one counted `validate_binding`; only an approved current binding re-arms its failed `provider_authentication_failed` job. A refused validation (401, 403, 404 or a non-retryable error) marks the binding `error`; approval is blocked while the last validation failed or the key is rejected.
- Through the form a rotation goes with a reconfiguration: it needs the full site attestation. A key-only save keeps the binding, region revision, approval and plant configurations ("API key replaced. Validate the connection to resume collection."); collection still waits for validation of each binding of the account. Changed setup values drop this plant's approval (new pending revision, first binding of a revision excepted). Lock order Region then ProviderAccount; clearing locks only the account. Concurrent rotations get serial revisions.
- Clearing is optimistic: the page carries `expected_cooldown_until` (microseconds kept) and `credential_revision`, and a NULL expectation cannot clear a delay recorded later. The callout shows only for a binding of the current revision with a future delay. It leaves the 401 block and the ledger alone. Natural expiry never nulls `request_blocked_until`, so only a manual clear lets throttled jobs run before their `available_at`.
- Keys: `encrypt_secret` accepts 1..1024 printable ASCII and encrypts with `MultiFernet` (HKDF-SHA256 of `SECRET_KEY`, context `pyracast-provider-credentials-v1`); fallbacks only decrypt. `reencrypt_secrets` reuses `encrypt_secret` without changing `credential_revision`.

## Tests

- `tests/test_provider_recovery.py` (console clear, stale and idempotent clearing, audit rollback), `tests/test_account_rotation.py` (operation rotation: write-only, platform-admin gate, input privacy, missing account, rollback, concurrency), `tests/test_admin_setup.py` (confirmation, form rotation and rollback, key-only save keeps approval, stale revisions), `tests/test_console_outcomes.py` (repeated clearance, named refusal), `tests/test_console_actions.py`, `tests/test_console_step_up.py`, `tests/test_console_failures.py` (confirmation and retry). Effects: `tests/test_collection_review_regressions.py`, `tests/test_collection_resilience.py`, `tests/test_revalidation_schedule.py`, `tests/test_delivery_health.py`, `tests/test_provider_secrets.py`.
- Gaps: the Playwright test opens the "Replace connection" disclosure but neither rotates a key nor clears a delay; no multi-plant account test through the form; no audit assertion on the form path; rotation of a disabled account through the form is unpinned.
