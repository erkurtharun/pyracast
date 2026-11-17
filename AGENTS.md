# Working agreement

Pyracast is a multi-tenant backend for authorized irradiance forecasts and daily energy estimates.
Start at `docs/README.md`: it indexes the stack reference, the per-flow maps and the data model. Read `docs/IRRADIANCE.md` when changing provider data or calculations.
Write code, documents and commits in English; explain progress and decisions in Turkish.

## Engineering

- Use CPython 3.15.2 and uv. Lock stable compatible dependencies; commit the lockfile. Verify upgrades deliberately; never silently downgrade Python or use prereleases.
- Prefer small cohesive functions, explicit boundaries, type hints and idiomatic framework features. Add abstractions only for actual needs.
- Keep HTTP, persistence and queue details outside calculations. Validate external input; preserve units, missingness and provenance.
- Keep views focused on access, input validation and serialization; place reusable selection and preparation policies in application functions. Use typed values at boundaries and avoid generic service or repository layers without a concrete need.
- Give frontend components one workflow to own. Group state that changes together, use discriminated unions for mutually exclusive async states and derive values instead of keeping duplicate state.
- Keep Windows and Ubuntu portability explicit and tested. Do not infer OS or live-provider support from mocks or metadata.
- Keep comments only for non-obvious contracts, units, external limits or hazards. No narrative comments, boilerplate docstrings or commented-out code.
- Keep one short source of truth per decision; update existing documents instead of creating routine reports or speculative plans.

## Workflow

- Find symbols, usages and problems through PyCharm MCP before reading source; read only needed lines. If IDE indexing cannot resolve a symbol, report that limit and use focused CLI searches.
- Do not read virtual environments, caches, htmlcov, build output or data directories as source context. Direct-read hooks enforce this without blocking runtime dependency access.
- For unknown or version-sensitive APIs, run `ctx7 library <name> <question>`, then `ctx7 docs <id> <question>`. Treat results as leads and verify important claims in current primary documentation. If Context7 is unavailable or its quota is exhausted, report the limitation and continue with official documentation.
- Before changing a function used elsewhere, refresh with `uv run --locked python scripts/agent_graph.py update`, then run `uv run --locked python scripts/agent_graph.py query callers_of <fn>` and `uv run --locked python scripts/agent_graph.py query tests_for <fn>`. Missing edges do not prove absence of callers.
- Use the shared graph wrapper in both clients; it keeps graph data in a per-user cache outside the repo, passes the same `CRG_HOME` to updates and queries, and honors an external absolute `CRG_HOME` override. Read the underlying CLI's `--help` when flags are unclear.
- Context7 and Code Review Graph are CLI-only: no Context7 MCP/setup; no graph MCP/install/daemon/watch/embed/wiki commands.
- Make the smallest complete change. Bug fixes include a regression test that fails before the fix. Test observable behavior at the relevant boundary.
- Complete authorized work autonomously; resolve routine choices from evidence. Preserve other writers' changes and escalate only missing authority or consequential unresolved choices.
- Commit and push completed, verified work to `main`. Merge temporary branches before removing them locally and remotely; keep only `main` after integration.
- Done means `uv run python scripts/check.py` is green. It runs format, lint, strict mypy, translation, ER-diagram, migration and Django checks, the API schema and web static checks when `web/node_modules` exists (otherwise it prints one `SKIPPED web` line), and parallel pytest with at most six workers unless `PYTEST_XDIST_AUTO_NUM_WORKERS` is set. `--static-only`, `--tests-only` and pytest arguments after `--` narrow a run during iteration (add `--no-cov` when selecting tests; the coverage floor applies to the whole suite). Never lower the measured coverage floor or weaken checks to obtain green.
- Use `uv sync --locked` for reproducible setup and `uv build --no-sources` for packaging. Shared quality settings live in `pyproject.toml`. Read `.agents/skills/verify/SKILL.md` for verification and `.agents/skills/writing-tests/SKILL.md` when choosing test boundaries; both clients use these same skill files.
- Verify affected tenant boundaries, authorization, transactions, concurrency, retries and numerical invariants. Use real PostgreSQL for database guarantees.
- Report actual checks, skips and limitations concisely. Keep credentials and local MCP configuration out of tracked files.

## Agent allocation

Orchestration, architecture and integration use the user's GPT-6.0 Ultra profile (`gpt-6-astra` when available), effort `ultra`; this does not change the selected client model.
Use `gpt-6.1-terra` at `medium` for simple bounded work or `high` for deeper bounded checks; use `gpt-6.1-sol` at `medium` for normal implementation/research, `high` for security/concurrency/mathematics/debugging, and `xhigh` only for exceptional unresolved complexity.
Use exposed model IDs and efforts; report unavailable profiles instead of silently substituting. Delegate independent work only when it saves time or improves quality, with clear ownership and acceptance criteria; the orchestrator reviews and integrates evidence.
These profile names apply to Codex. Keep the selected Claude profile unless the user explicitly requests another.
