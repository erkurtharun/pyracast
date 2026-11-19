# Ideal-state program: progress

Resume point for the orchestrator after a context reset. Keep it short and current.
The repository is public and production is live, so this file names findings by id
only; their descriptions stay in the local working notes until the fix is released.

## Goal

Map every flow end to end, document it durably, then bring the codebase to its ideal
state (clean code, security, performance, resilience, ease of use). Five workflows run
in order: Stack → Flow map → Evaluate → Implement → Final scan. Each workflow ends with
an independent review (a Claude reviewer that did not write the work, plus Codex CLI).

## Ground rules for every agent

- Each pull request is built on its own short-lived branch `pr/<id>` in its own git
  worktree under `%LOCALAPPDATA%\pyracast\worktrees\<id>`, cut from `origin/main`. Work
  only in the worktree named in your brief. You may commit locally on that branch when
  your brief says so, and push that one `pr/<id>` branch only when your brief is to ship
  it. **Never merge, switch branches, push any other ref or dispatch a workflow**: a
  push to `main` starts the production release. Only the orchestrator merges; an
  untracked git hook on its workstation refuses every other push from agent sessions.
  While the program runs, these rules take precedence over the general push rule of
  `AGENTS.md` for every agent working for it.
- The main checkout holds the working notes under `docs/agents/work/` and is never
  switched. Read briefs and write notes there by absolute path. Use absolute paths for
  every write: .NET file calls in PowerShell resolve relative paths against the process
  directory (the main checkout), not the current location.
- Never run `git clean`, `git reset --hard`, `git stash` or `git checkout -- <path>` on
  files you do not own.
- Never touch `.env`, `secrets/`, `.dev/backups/`, any PostgreSQL data directory, or
  anything under `%LOCALAPPDATA%\pyracast\` outside your own worktree. Do not start or
  stop the dev stack or PostgreSQL.
- No comment in code unless it carries critical information or an important business
  rule; delete comments that break this rule in the files you touch. Documents stay
  short, generic, clear and precise: edit existing text instead of adding to it.
- Pull requests do not edit `docs/flows` pages: each records the pages it makes wrong in
  the local `docs/agents/work/DOC-DEBT.md`, and the final documentation package rewrites
  them once. Other documents change only where an instruction would be wrong or unsafe.
- Review-bot comments are handled in one round: correctness and security are fixed,
  wording and style go to the documentation debt.
- Code, comments, docs, commit messages and agent output files are English. Locale
  files (`web/messages/tr`, `locale/tr`) stay Turkish.
- Take nothing at face value: cite `path:line` for every claim about the code and say
  which claims you did not verify. No agent approves its own work.
- Never weaken a check to get green: no new coverage pragma, `xfail`, `skip`,
  `type: ignore`, `noqa`, `eslint-disable` or scanner exclusion, and no deleted or
  loosened assertion, unless your brief names it. A pin that does not fail for the
  stated reason stops the work: report it, never bend the test.
- Commit messages, test names, test bodies and tracked documents describe the new
  behaviour, not the weakness it replaces.
- Write details to the file named in your brief and return only a short summary.
  Never use the Artifact tool and never publish, upload or send anything outside this
  machine.
- A worktree has no `.env`. Database tests need `PGHOST=127.0.0.1`, `PGPORT=55434` (the
  throwaway test cluster; never the development cluster on 55433), `PGUSER=pyracast`
  and their own `PGDATABASE=pyracast_<your-label>`; drop every database you created
  when done. Use `--no-cov` for targeted runs; only the full
  `scripts/check.py` run measures coverage, and only the orchestrator schedules it.
- On Windows run pytest and PostgreSQL tools from PowerShell, not Git Bash, and never
  pipe or wait on `pg_ctl start` (the server inherits the handle and the call never
  returns).

## Environment facts (verified 2026-11-17)

- Dev PostgreSQL 18.6 runs from `%LOCALAPPDATA%\pyracast\postgres\data` on
  `127.0.0.1:55433`, binaries in
  `%LOCALAPPDATA%\pyracast\postgres\pgsql\bin`.
- Verified backup: `.dev/backups/ideal-state-20261116T223359Z/` (custom-format dump,
  raw `COPY` export of every table, fingerprints, manifest), mirrored to
  `%LOCALAPPDATA%\pyracast\backups\`. The restore reproduced all 51 tables, 3,364 rows,
  every sequence position and the schema catalogs.
- RabbitMQ still runs from `%TEMP%\pyracast-rabbitmq-0l71hcer` (cleanup hazard; it holds
  no authoritative state).
- The dev app services (api, frontend, worker, mailer, dispatcher) are stopped (memory
  is tight: 16 GB). PostgreSQL and RabbitMQ stay up. Stop and start through the
  launcher only: `uv run --locked python -m scripts.dev stop` or `start`.
- Codex CLI 0.169.0: `gpt-6.2-sol` (routine review) and `gpt-6.2-astra` (critical
  decisions) both answer. Review call, run from the worktree and asking for English:
  `codex exec -m <model> -c model_reasoning_effort=high -s read-only -o <file> "<prompt>"`.
- Qodana CLI 2026.2.3 is installed; `sonar-scanner` and Java are not (SonarCloud runs
  in CI, including on pull requests).
- The full gate runs against a throwaway non-durable test cluster on `127.0.0.1:55434`
  (`docs/agents/work/tools/test_cluster.py init|start|stop|status`; it mirrors the CI
  cluster), never against the development cluster: there `DROP DATABASE` waits for slow
  checkpoints and the run took 84 minutes with teardown timeouts. On the test cluster
  it takes about 25 minutes with six workers. Set `PGHOST`, `PGPORT=55434` and `PGUSER`
  for the run, one run at a time. Coverage floor is 100.
  With `-n auto` on the old gate the run was stopped for lack of memory.
- CI takes about 14 minutes per run and the release about 8. The local time zone is
  Europe/Istanbul.
- Mermaid validation lives outside the repository
  (`node <scratchpad>\mermaid-check\check.mjs docs`, Mermaid 11.17.2).

## Status

| Workflow | State | Output |
| --- | --- | --- |
| 0. Backup and verified restore | done | see above |
| 1. Stack reference | done, merged | `docs/STACK.md`; 108 confirmed deviations in the working notes (3 high: NU-D1, CS-1, CS-2) |
| 2. Flow map and ER diagram | done, merged (PR #13) | `docs/flows/` (81 pages and index), `docs/DATA_MODEL.md` generated by `scripts/generate_er_diagram.py`, `docs/README.md` |
| 3. Evaluation | done: 81 flow evaluations and 10 system lenses, each adversarially verified (1,447 surviving findings, 36 high), 13 group syntheses (414 work items), target design, roadmap, three independent reviews (Codex Astra, feasibility, pre-mortem) and a code-checked readiness brief for each wave-1 batch | working notes: `evaluation/TARGET.md`, `ROADMAP.md` (113 batches, rules 1 to 15, wave table), `DECISIONS.md`, `REVIEW-LOG.md`, `readiness/<batch>.md` |
| 4. Implementation | in progress: 152 work packages in eight lanes (`evaluation/PLAN.md`), grouped into 52 bundles of one pull request each (`evaluation/BUNDLES.md`; two added on owner request, two for defects found in operation, one for scanner findings in the newly analysed deployment files): 24 merged and released by 2026-11-19 (K01 to K10, K04b, K12 to K18, K46 to K51), 4 in work (K11, whose first half is released, K19, K20, K21). The production database was reset to the regenerated baseline on 2026-11-18 (`aa54616`); schema changes after it are forward migrations. State per pull request is in the ledger | pull requests to `main`; `evaluation/LEDGER.md` |
| 5. Final scan (Qodana, Sonar) | pending | |

All paths in the Output column that start with `evaluation/` are under
`docs/agents/work/`.

## How one pull request runs

One work package of the plan is one pull request.

1. The orchestrator creates the worktree (`docs/agents/work/tools/new_worktree.ps1`).
2. An implementing agent commits the pins first (tests that fail for the stated reason,
   or characterisation tests), then the change with its documentation. Locally it runs
   the static checks and the targeted tests only.
3. Claude models write the code, so the review is done by one model only, the Codex CLI
   (`gpt-6.2-sol`; high-risk packages `gpt-6.2-astra`). It also checks that the pins fail
   on the unchanged code and lists every changed assertion and new suppression. Findings
   are verified before they are applied; a high-risk package gets a final Codex
   verification after the fixes, and a Codex review of every production change the
   shipping step makes afterwards.
4. Claude agents ship it (`docs/agents/work/tools/ship-claude.js`): Sonnet rebases, pushes
   the branch and opens the pull request, Haiku only waits for the run, Sonnet resolves
   failing checks, Sonar issues and review-bot comments in the code, and Opus takes over
   only when Sonnet could not. The pull request's CI run is the full gate (both operating
   systems, coverage floor 100, Sonar).
5. The orchestrator merges with one squash commit per pull request, in trains: every
   pull request whose CI is green is merged back to back and `main` runs CI and the
   release once for the train (superseded runs are cancelled). At most one pull request
   with a migration or a deploy-path change per train. A red `main` or a failed release
   stops merging; the faulty squash commit is reverted.
6. The orchestrator records the row in the ledger, watches CI and the release on
   `main`, and removes the worktree and branch.

## Working notes stay local

`docs/agents/work/` is git-ignored: candid defect and security observations are not
published before they are fixed. The notes are mirrored to
`%LOCALAPPDATA%\pyracast\backups\agents-work\` at each milestone; the final report
summarises what was found and changed.

## Backlog noticed outside a workflow

- A detached worktree `%LOCALAPPDATA%\pyracast\worktrees\sonar-approval-fix` predates
  this program and is not ours; ask the owner before removing it.
- Repo-root clutter: `UserserkurAppDataLocalcode-review-graphpyracast/` (a mis-resolved
  graph cache path, 80 MB), empty `tools/` (B11).
- Durable dev dependencies and the launcher's npm lookup are B10; the bounded gate is
  B01.

## Owner decisions

2026-11-17:

- Pushing to `main` and the automatic production release on green CI are intended and
  stay. Consequence: everything merged must be production-safe. Schema changes are
  forward migrations that work on existing data; no migration squash or data reset
  that production cannot follow.
- Work may go through pull requests. When the program ends only `main` remains,
  locally and on the remote.
- Dependabot pull requests are resolved by best practice and merged, not left open.
- OD-30: yes. A merge that ships nothing (documentation or tests only) skips the
  production release; code changes keep releasing automatically (B08a).
- The local full gate may run with a bounded worker count.
- The repository ruleset `main` targets the default branch: branch deletion and
  non-fast-forward pushes to `main` are refused.
- Every other owner decision in `DECISIONS.md` takes the recommendation as the working
  default, except those that cost money or cannot be undone.
- OD-41: yes. Tranche 1 (36 batches) is committed; the remaining 77 batches are ranked
  again at a checkpoint after it.
- The release skip list may also hold other paths that ship nothing (`scripts/`, agent
  and scanner configuration, the ignore files), each with proof that no image or host
  bundle contains it.
- No quiet hours beyond the 15:00 to 16:30 Europe/Istanbul rule; the owner is reachable
  at any hour for tier A merges.
- The Qodana scans may use the project token, stored as the user-level environment
  variable `QODANA_TOKEN` on this machine. Never print it or write it to a file.

- No backward compatibility (said after the estimate of four to six weeks): anything
  may be refactored, renamed or deleted. Old API shapes, names, stored values, routes
  and schema are not kept for compatibility; staged migrations, aliases, shims and
  deprecation periods are dropped. This replaces the earlier consequence "no migration
  that production cannot follow in compatible steps". What stays: a release must
  succeed on the production database, tests first, independent review, green gate and
  CI. The roadmap is being revised for this (run recorded in the ledger).

- The production database may be reset too (after a backup whose restore is verified).
- The work must finish much faster. The orchestrator therefore changed how it runs:
  one pull request per work package instead of a series; the pull request's CI run is
  the full gate (it runs `scripts/check.py` on both operating systems), locally only
  targeted tests, static checks and the coverage of changed modules; review by one
  Claude reviewer and Codex in parallel, a final verifier only for high-risk pull
  requests; merge as soon as CI is green, back to back, with no quiet hours; up to
  eight packages in work at once. A red `main` or a failed release still stops merging.
  The plan for this is `evaluation/PLAN.md` (it replaces the roadmap's rules, order
  and staging); the pipeline script is `docs/agents/work/tools/implement-fast.js`.

- Reset of the production database (answered ~20:15): the raw data pulled from Solcast is
  kept as a file next to the pre-reset backup (raw responses with their collection time
  and site parameters); nothing is loaded back into the new database.
- Scope: core work first (security, data integrity, operations, correctness, error
  handling), then the polish and feature bundles too; all of them are done.
- Login limits: the daily lock per address stays (a password reset lifts it and the owner
  is notified); the shared hourly budget for mails to unknown or existing addresses is 20.

2026-11-18:

- The orchestrator carries out the owner's steps on the production host itself (IAP
  through the Cloud SDK on this machine): the reset runbook, `terraform apply` after
  infrastructure changes, manual Release dispatch. Steps that create or reveal a secret
  the owner must hold stay with the owner.
- Never use Fable models for agents; Opus implements, Codex reviews (one model per
  package: `gpt-6.2-sol` routine, `gpt-6.2-astra` high risk), Claude agents ship each
  pull request. Tokens are spent with care: the smallest model and effort that fit the
  step, no agent while a run only has to be waited for.
- No code comment unless it carries critical or business information; documents stay
  short. Flow pages are corrected once, by the final documentation package, from
  `docs/agents/work/DOC-DEBT.md`.
- Priority over the other bundles: the platform administrator manages workspaces from
  the console (create, settings, invite and remove members, add a plant with its
  details). It is bundle K46; trash, reminders, production import and outages stay in
  the customer application.

## Open questions for the owner

2026-11-19:

- Codex reviews use `gpt-6.2-sol` and `gpt-6.2-astra` only; no verdict of an older
  model is accepted. When Codex cannot be reached or its quota is spent, the review
  goes to the Antigravity CLI (Gemini 4 Neon for hard work, Gemini 4 Hydrogen for
  simpler work) and the ledger names the model behind the verdict.

Owner step after the reset: enter the Solcast provider account again. The first
administrator exists with its second factor (2026-11-18).

Explicit answers needed, none of them blocking the work in progress:

- OD-31: scanner licence after the baseline scan (the trial ends 2026-12-12).
- OD-30, last part (with B08).
- OD-39 (copy of the working notes off this machine), OD-40 (disposable VM to rehearse
  installer changes), OD-08, OD-09, OD-27, OD-28, OD-29, OD-32.

## Other writers

`main` received commits from the owner while this program ran. Fetch before every
branch and before every merge; never overwrite upstream work.
