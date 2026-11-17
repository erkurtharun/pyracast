# Rotate a provider API key or clear a provider cooldown

A platform administrator recovers a shared Solcast account in two separate actions. Saving the plant's setup with a replacement key and an explicit confirmation rotates the encrypted key and bumps `credential_revision`, which lifts a 401 block but makes every plant on the account need a new validation; with otherwise unchanged setup values the binding and approval stay. After the provider confirms recovery, a second confirmed action clears the 429 delay. Both are audited per account.

## Entry points

- Console: `POST /admin/plants/{id}/` (`PlatformAdminSite.plant` delegates; also `/admin/regions/region/{id}/setup/`, which the tests use) -> `pyracast.admin.RegionAdmin.setup`; template `src/pyracast/regions/templates/admin/regions/region/setup.html`.
- Rotation: `action=configure`, `account_mode=existing`, `api_key`, `confirm_key_rotation` -> `pyracast.admin_forms.ProviderSetupForm.configure` -> `pyracast.connections.operations.rotate_secret` -> `pyracast.connections.secrets.encrypt_secret`; the "Replace connection" disclosure.
- Cooldown: `action=clear_cooldown` + `provider_recovered` -> `BindingActionForm.current_binding` -> `pyracast.connections.recovery.clear_provider_cooldown`; the "Provider request delay" callout.
- `POST /api/platform/accounts/{a}/rotate-secret/` -> `pyracast.api.views.RotateSecretView`: same `rotate_secret`, no confirmation, no expected revision, no web caller, hidden from the customer OpenAPI schema.

## Sequence

```mermaid
sequenceDiagram
    actor A as Platform admin
    participant V as RegionAdmin.setup and forms
    participant O as Operations
    participant DB as PostgreSQL
    participant W as Dispatcher and worker
    A->>V: "POST configure with api_key and confirm_key_rotation"
    V->>DB: "BEGIN, Region FOR UPDATE, check expected_revision and expected_binding_id"
    V->>DB: "enabled Solcast ProviderAccount FOR UPDATE, check key revision from the page"
    V->>O: "rotate_secret"
    O->>DB: "encrypted_api_key replaced, credential_revision + 1, audit provider_secret_rotated"
    V->>O: "configure_binding"
    O->>DB: "unchanged setup: existing SiteBinding; changed with a binding of the revision: region revision + 1 and pending, new SiteBinding, audit; first binding: new SiteBinding at the current revision, audit; COMMIT"
    alt stale revision, invalid key or binding rule
        V-->>A: "400 field or generic setup error, rotation rolled back"
    else saved
        V-->>A: "302 to /admin/plants/{id}/"
    end
    Note over DB,W: Other bindings of the account stop matching credential_revision and claim marks their jobs stale
    A->>V: "POST clear_cooldown with provider_recovered"
    V->>DB: "current_binding checks region revision and key revision"
    V->>O: "clear_provider_cooldown, which locks ProviderAccount FOR UPDATE"
    alt key revision or expected_cooldown_until differs
        V-->>A: "400 generic setup error"
    else no delay stored
        V-->>A: "302 success message, nothing written"
    else delay matches
        O->>DB: "request_blocked_until NULL, audit provider_cooldown_cleared, COMMIT"
        V-->>A: "302 Provider wait cleared"
        W->>DB: "pending_job_ids selects provider_throttled retry jobs, claim runs current ones early"
    end
    A->>V: "POST validate per plant, one counted request, refused unspent while a delay is active"
    A->>V: "POST approve this plant again: configure left it pending without approved_binding"
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| `connections_provideraccount` | R/W | Locked `FOR UPDATE`; rotation writes `encrypted_api_key` and `credential_revision`, clearing writes `request_blocked_until`; `authentication_failed_revision` and `daily_quota` are never touched |
| `connections_sitebinding` | R/W | Form rotation with unchanged setup keeps the binding; with changed setup it inserts a new one (full `configure` write); other bindings are only compared via `validated_credential_revision` |
| `regions_region` | R/W | Locked by `configure`; `revision`, `status` and `approved_binding` change only when the setup changed and the region already has a binding for its current revision |
| `regions_auditevent` | W | `provider_secret_rotated` and `provider_cooldown_cleared` (account target, `revision` = `credential_revision`); `binding_configured` (region target) |
| `forecasts_collectionjob` | R | `claim` and `pending_job_ids` read the account delay; rotation and clearing write no job (only `validate_binding` re-arms, see `provider-connection-setup`) |

## Authorization

- Console: `PlatformAdminSite.has_permission` requires active staff, `is_superuser` and `has_verified_mfa`; `admin_view` adds CSRF. Nothing narrower exists per action.
- API: `PlatformAdmin` (active superuser plus MFA, no staff check) replaces the default permissions; session authentication and CSRF. Operations: `require_admin` (active superuser only) in `rotate_secret` and `clear_provider_cooldown`.
- Database: `provider_account_encrypted_api_key` check requires the `encrypted:v1:` prefix; `audit_exactly_one_target` makes account audit rows target no region. Accounts are platform-wide, shared across tenants.

## Failure paths

| Condition | Result |
| --- | --- |
| Not staff, superuser or MFA / missing CSRF | Console 302 to `/admin/login/`, which redirects to `/app/?next=/admin/` / 403; API 403 `http_403` |
| Key on an existing account without confirmation; clear without `provider_recovered`; key revision changed or account disabled before the POST (choices are rebuilt per request) | 400 with a field error ("Select a valid choice" for the account), key never re-rendered, nothing written |
| Key over 1024 characters or not printable ASCII; API key empty | Console: length is a field error, spaces or other characters the generic message (an empty key on an existing account is no error: the binding saves without rotation); API: `http_400` from the serializer, `invalid_request` from `encrypt_secret` |
| Stale region or binding revision; key revision or account changed after the form was built; binding rule violation | 400 "The setup could not be applied..." (specific messages swallowed); rotation rolled back |
| Clear with a changed cooldown, region revision, binding or `credential_revision` | 400 generic message under "Validate and approve", nothing cleared |
| Cooldown already NULL | 302 "Provider wait cleared..." though nothing was written |
| Lock wait over `lock_timeout` (5 s) | Console unhandled `OperationalError` (500, inferred); API 503 `workspace_busy` |
| API account missing | 404 `http_404` from `get_object_or_404`; `not_found` only if the row vanishes before the lock |

## Rules and invariants

- Rotation never touches `request_blocked_until` or `RequestAttempt`: a 429 delay survives a new key and the daily budget is unchanged. The 401 block lifts only because `authentication_failed_revision` no longer equals `credential_revision`; `record_provider_failure` ignores errors from an older revision.
- Every binding of the account stops being current (`binding_is_current` compares `validated_credential_revision`). `claim` turns their jobs `stale`, `approve_region` refuses them, `remaining_forecast_requests` skips them and customers see `freshness: "stale"`. Each plant needs one counted `validate_binding`; only an approved current binding re-arms its failed `provider_authentication_failed` job.
- Through the form a rotation goes with a reconfiguration: it needs the full site attestation. A key-only save keeps the binding, region revision, approval and plant configurations ("API key replaced. Validate the connection to resume collection."); collection still waits for validation of each binding of the account. Changed setup values drop this plant's approval (new pending revision, first binding of a revision excepted). Lock order Region then ProviderAccount; the API rotation and clearing lock only the account. Concurrent rotations get serial revisions.
- Clearing is optimistic: the page carries `expected_cooldown_until` (microseconds kept) and `credential_revision`, and a NULL expectation cannot clear a delay recorded later. The callout shows only for a current-revision binding with a future delay. It leaves the 401 block and the ledger alone. Natural expiry never nulls `request_blocked_until`, so only a manual clear lets throttled jobs run before their `available_at`.
- Keys: `encrypt_secret` accepts 1..1024 printable ASCII and encrypts with `MultiFernet` (HKDF-SHA256 of `SECRET_KEY`, context `pyracast-provider-credentials-v1`); fallbacks only decrypt. `reencrypt_secrets` reuses `encrypt_secret` without changing `credential_revision`.

## Tests

- `tests/test_provider_recovery.py` (console clear, stale and idempotent clearing, audit rollback), `tests/test_account_rotation.py` (API rotation, MFA, CSRF, input privacy, missing account, rollback, concurrency), `tests/test_admin_setup.py` (confirmation, form rotation and rollback, stale key revision). Effects: `tests/test_collection_review_regressions.py`, `tests/test_collection_resilience.py`, `tests/test_revalidation_schedule.py`, `tests/test_delivery_health.py`, `tests/test_provider_secrets.py`, `tests/test_admin_mfa_bootstrap.py`.
- Gaps: no Playwright test of rotation or clearing; no multi-plant account test through the form; no audit assertion on the form path; rotation of a disabled account is unpinned; the API 404 body code is not asserted.
