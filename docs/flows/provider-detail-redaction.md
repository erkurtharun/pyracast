# Hide provider details from customer API responses

Every Django REST Framework (DRF) response of a project view is rendered by `CustomerJSONRenderer`. Unless the request path starts with `/api/platform/`, the renderer first rebuilds the payload with `public_source_data`, which drops private fields, removes or renames provider-named keys and replaces provider names inside technical strings. Stored rows are never changed; only the bytes sent to the client are.

## Entry points

- `pyracast.api.renderers` `CustomerJSONRenderer.render`: the only production caller. It is the sole entry of `DEFAULT_RENDERER_CLASSES` in `pyracast.settings`, and no project view overrides `renderer_classes`, so it renders every project DRF view, success and error alike (error bodies come from `pyracast.api.errors` `exception_handler`).
- `pyracast.api.public` `public_source_data`: the recursive projection; called only by the renderer in `src/` and directly by tests.
- Not covered: `SpectacularAPIView` at `/api/schema/` (drf-spectacular sets its own OpenAPI renderers; platform paths are filtered by `pyracast.api.schema` `customer_endpoints`), allauth headless routes under `/api/auth/` including the project overrides (allauth views, not DRF), the social login callbacks, the `JsonResponse` of the `pyracast.accounts.session_lock` middleware and of `pyracast.admin_historical`, the Django admin, health checks, email and the browser-built CSV.
- Actor: none; it runs for every authenticated or anonymous API caller, including platform administrators (who are exempt on `/api/platform/`).

## Sequence

```mermaid
sequenceDiagram
    actor U as Client
    participant V as DRF view
    participant H as exception_handler
    participant R as CustomerJSONRenderer
    participant P as public_source_data
    U->>V: "GET or POST /api/..."
    opt view raises
        V->>H: "exception (APIView.handle_exception)"
        H-->>V: "Response with code and detail, or None"
    end
    V-->>R: "Response.rendered_content calls render(data, context)"
    alt "request is None or path does not start with /api/platform/"
        R->>P: data
        P->>P: "walk Mapping, list and tuple, build new containers"
        Note over P: "drop private keys, drop or rename solcast keys, rewrite technical strings"
        P-->>R: projected copy
    else platform path
        Note over R: "data passes unchanged"
    end
    R-->>U: "JSON bytes"
```

## Data

| Table | R/W | What |
| --- | --- | --- |
| None | - | Pure in-memory transformation of the response payload. It reads and writes no table, queue or external service. It projects JSON columns that other flows store (`ForecastResult.payload`, `CalibrationRun.dataset_manifest` and `result`) and leaves them untouched. |

## Authorization

- None in this flow. Access control is the view's (`tenant-authorization`); the renderer only shapes what an already authorized caller sees.
- The exemption is decided by URL prefix alone, not by user. The `/api/platform/` views are guarded by `pyracast.api.views` `PlatformAdmin` (active superuser with verified MFA via `pyracast.accounts.security` `has_verified_mfa`), not by the renderer; a refused caller gets an unredacted DRF 403 error body.
- Fail-closed default: a missing request in the renderer context is redacted like a customer request.

## Failure paths

| Condition | Result |
| --- | --- |
| No `request` in the renderer context | Redacted as a customer response |
| Path is `/api/platform/...` | Unredacted; those views return `{"id": ...}`, the region output of `TransitionView`, or error bodies |
| Private key at any depth (`_PRIVATE_FIELDS`) | Key and value removed, no marker |
| `provider` key whose value is not a string (also `None`), or is `solcast` or `fixture` (case-folded) | Key removed; any other string, such as `open_meteo`, is kept |
| Key containing `solcast` other than `solcast_ghi_wm2`, `solcast_kwh_m2` | Key removed; those two become `reference_ghi_wm2`, `reference_kwh_m2` |
| String containing `solcast` inside a technical subtree | Whole string equal to `solcast_forecast` or `solcast_estimated_actuals` becomes `forecast` or `estimated_actuals` (under any technical key); a string directly under `quality_flags` gets `reference` for the word; anything else becomes `source_details_unavailable` |
| Error body whose `detail` mentions the provider | String becomes `source_details_unavailable`; the web client drops snake_case tokens without a message key and falls back to the code message or a generic one (`web/src/lib/api.ts` `errorMessage`) |
| Provider name in an unlisted field outside a technical subtree (for example `label`) | Passes through unchanged |
| View raises an unmapped exception other than `SuspiciousOperation` or `BadRequest` | The handler answers 500 `{code: "server_error", message, request_id}`, which passes through this renderer like any other body |
| Redaction raises inside `render` | Not handled by the exception handler; Django answers 500 (reasoned, not exercised by any test) |

## Rules and invariants

- Denylist, not allowlist: a new field is public until its name is added to `_PRIVATE_FIELDS`, and a string is rewritten only below a key in `_TECHNICAL_FIELDS` (`result`, `provenance`, `vintage`, `detail`, `code`, `source` and others). Matching is on the case-folded key (`str(key)` for integer keys); the walk follows `Mapping`, `list` and `tuple` only and turns tuples into lists. Only the substring `solcast` is detected.
- `technical` is sticky: once inside a technical subtree every descendant string is rewritten. Exempt and copied verbatim, even if they say "Solcast": `name`, `username`, `email`, `recipient` while `technical` is false, and the `source` of an observation (a mapping with `source` and `starts_at` or `observation_id`) at any depth, technical or not.
- Inside a technical subtree user-text keys are rewritten too (`{"result": {"name": "Solcast"}}` yields `source_details_unavailable`); `tests/test_public_data.py` pins this.
- The projection builds new dicts and lists; the stored payload stays intact (pinned by `tests/test_public_data.py`). The renderer rebinds only its local `data`, so anything that reads `response.data` (test client, middleware) sees the unredacted value.
- Two keys that map to the same public key (`solcast_ghi_wm2` and `reference_ghi_wm2`) overwrite each other in iteration order.
- Internal numeric ids such as `binding_id` and `snapshot_id` and the `binding_revision` value are not private (no recorded decision); a test pins `provenance.binding_id` as public.
- Open-Meteo is the licensed attribution exception (`docs/ARCHITECTURE.md`): its `provider` value `open_meteo` and `attribution` reach the customer because only `solcast` and `fixture` are dropped.

## Tests

- `tests/test_public_data.py`: user text and production attribution preserved, legacy `source_kind` aliases, `quality_flags` rewrite, user text rewritten inside a technical subtree, `provider: None` dropped, renderer behavior for customer, platform and missing-request paths (built with `APIRequestFactory`), calibration detail and archived forecast over the real API with a check that stored rows are unchanged, credential removal at any depth. Its email test checks `notifications.mail` `build_message`, which does not use this projection.
- `tests/test_public_schema.py`: the customer schema excludes `/api/platform/` and contains no provider name. `tests/test_midnight_forecast_fallback.py` pins `provenance.binding_id` in a public response. `web/tests/public-site.spec.ts` checks marketing pages only.
- Gaps: no test sweeps every customer endpoint for provider names; none for a new unlisted field, for `provider: fixture`, for the key collision, for a real request to `/api/platform/`, or for deployment under a script prefix; no Playwright test sees a redacted API payload.
