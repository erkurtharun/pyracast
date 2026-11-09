---
name: verify
description: Verify Pyracast code or tooling changes and distinguish local checks from unverified provider and platform behavior.
---

Use `uv run --locked python tools/check.py` for the shared formatting, lint, type
and test checks. Build packaging changes with `uv build --no-sources`; CI also
imports the resulting wheel outside the checkout. Settings belong in
`pyproject.toml`, not duplicated command-line policies.

During iteration, run affected tests first. Use the full suite for coverage
claims; a targeted run can use `--no-cov` without changing shared configuration.
Inspect `pytest -ra` skip reasons and report unrun checks accurately.

Choose evidence at the changed boundary:

- Tenant access and approval: authenticated API calls by two tenants and an admin.
- Transactions, leases, quotas and idempotency: real PostgreSQL, concurrent writers
  and restart/redelivery cases; mocks cannot prove database behavior.
- Provider adapters: captured or documented fixtures, timeouts and quota accounting;
  identify separately whether authorized live requests were tested.
- Calculations: read `docs/IRRADIANCE.md`; check units, missing intervals, timezone
  boundaries and numerical invariants using independent reference values.
- CI/configuration: validate syntax, execute corresponding local commands and
  inspect an actual workflow run when available. Windows results do not prove Ubuntu.

Review the diff and relevant callers. Report concrete defects and missing evidence
without mandatory review labels or extra process. Keep the handoff short and in Turkish.
