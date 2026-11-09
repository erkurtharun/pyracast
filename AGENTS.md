# Working agreement

Read `docs/ARCHITECTURE.md` for scope. Read `docs/IRRADIANCE.md` when changing provider data or calculations. Load only context relevant to the current task. Write code and project documents in English; explain progress and decisions to the user in Turkish.

## Engineering

- Prefer the simplest maintainable design that satisfies the contracts. Use idiomatic framework features, explicit boundaries, type hints, and small cohesive functions. Add abstractions when they solve an actual problem.
- Keep HTTP, persistence and queue details outside numerical calculations. Validate external input and preserve units, missingness and provenance.
- Research uncertain APIs, compatibility and security behavior in current primary documentation. Compare alternatives when the decision matters. Adopt a materially better approach when evidence supports it; preserve business requirements and explain significant changes briefly.
- Use CPython 3.15.2. Resolve the latest mutually compatible stable dependencies at bootstrap, then commit the lockfile. Verify upgrades deliberately; do not silently downgrade Python or use prereleases.
- Keep application code portable across Windows and Ubuntu. Test portability instead of inferring it from language choice or package metadata.

## Comments and documentation

- Prefer expressive names and straightforward code. Comments and docstrings should explain only non-obvious contracts, business decisions, units, external limitations or important hazards.
- Do not narrate the code, add boilerplate docstrings, commented-out code, change-history comments or decorative sections.
- Maintain one short source of truth for each decision. Update existing documentation when behavior changes; avoid duplicate plans, routine task reports and speculative documentation. Let types, tests and generated API schemas carry details they already express.

## Execution and verification

- Use `uv sync --locked` and `uv run --locked python tools/check.py`; build with `uv build --no-sources`. Quality settings live in `pyproject.toml`. Repository skills `.agents/skills/verify` and `.agents/skills/writing-tests` provide task-specific guidance; load them when relevant.
- Deliver working vertical slices, including meaningful tests. Verify tenant boundaries, authorization, transactions, concurrency, retries and numerical invariants where affected.
- Use formatting, linting, static checks and an appropriate test suite. Do not replace meaningful integration tests with mocks of every boundary.
- Inspect failures and fix their causes. Report checks actually run and any remaining limitation. Do not claim live-provider accuracy or OS support without evidence.
- Keep updates and handoffs short: outcome, relevant files, checks and unresolved issues. Avoid dumping full files or tool output.

## Autonomy

Complete authorized work through implementation and verification. Resolve routine choices from code, contracts, tests and primary documentation; state material assumptions and continue independent work when one boundary is blocked. Preserve other writers' changes.

Repository guidance does not add tool allowlists or approval ceremonies. Use the capabilities granted by the client/session. Escalate only missing authority or a consequential unresolved choice, such as destructive operations or production changes. Do not weaken tests, coverage, typing or authorization to make checks pass; update requirements and their checks together when behavior intentionally changes.

## Agent allocation

| Role | Model/profile | Effort |
| --- | --- | --- |
| Orchestration, architecture and integration | User's GPT-6.0 Ultra profile (`gpt-6-astra` when that mapping is available) | ultra |
| Simple, well-scoped work | GPT-6.1 Terra | medium |
| Bounded work needing deeper checking | GPT-6.1 Terra | high |
| Normal implementation or targeted research | GPT-6.1 Sol | medium |
| Security, concurrency, mathematics or difficult debugging | GPT-6.1 Sol | high |
| Exceptional unresolved complexity | GPT-6.1 Sol | xhigh |

Use the lowest suitable effort within this allocation. Delegate independent work when it saves time or improves quality; avoid duplicate analysis and unnecessary agents. Give each agent a clear task, acceptance criteria and relevant files. Coordinate ownership before parallel edits. The orchestrator reviews evidence and integrates results.

Use model IDs and effort values actually exposed by the installed Codex client. If a requested profile is unavailable, report the mismatch rather than inventing an ID or silently substituting. These instructions guide allocation; they do not configure the client's selected main model.
