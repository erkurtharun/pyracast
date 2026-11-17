---
name: writing-tests
description: Choose meaningful test boundaries for Pyracast tenant operations, durable collection jobs, provider adapters and irradiance calculations.
---

Test observable behavior at the narrowest boundary that proves it. Keep fixtures
local unless shared and build test subjects inside tests or fixtures. Shared builders,
fakes and query budgets live in `tests/support/`; a test module never imports another.
`tests/conftest.py` marks modules
`tooling`, `subprocess`, `migration`, `browser` or `live` for selection. Tests are
included in strict mypy. Shared pytest and coverage settings live in `pyproject.toml`.

Keep numerical tests independent of Django, HTTP and queues. Use independently
computed references and invariants from `docs/IRRADIANCE.md`, including incomplete
days, interval energy conservation and daylight-saving transitions. Never derive
expected values by calling the implementation under test.

Use real PostgreSQL for transactional, uniqueness, quota and concurrency claims.
Exercise all writers of an invariant, including admin actions and workers. Cover
tenant isolation with separate authenticated identities; checking only list
filters does not prove detail, mutation or worker isolation.

Provider fixtures preserve units, missingness, timestamps and provenance. Fake
external I/O at the adapter boundary rather than mocking every internal operation.
Fixtures do not establish live-provider accuracy.

For durable jobs, cover duplicate delivery, interrupted leases, stale revisions,
retry budget consumption and publication after approval changes. Synchronize
concurrent tests with events and use bounded timeouts rather than arbitrary sleeps.

Regression tests should fail before the fix. Assert exact public outcomes, not
private call counts. Environment skips name the required resource; configured but
unreachable dependencies should fail rather than silently skip.
