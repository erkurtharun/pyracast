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
  your brief says so. **Never push, merge, switch branches or dispatch a workflow**: a
  push to `main` starts the production release. Only the orchestrator pushes and
  merges; an untracked git hook on its workstation refuses pushes from other agent
  sessions. While the program runs, these rules take precedence over the general push
  rule of `AGENTS.md` for every agent working for it.
- The main checkout holds the working notes under `docs/agents/work/` and is never
  switched. Read briefs and write notes there by absolute path. Use absolute paths for
  every write: .NET file calls in PowerShell resolve relative paths against the process
  directory (the main checkout), not the current location.
- Never run `git clean`, `git reset --hard`, `git stash` or `git checkout -- <path>` on
  files you do not own.
- Never touch `.env`, `secrets/`, `.dev/backups/`, any PostgreSQL data directory, or
  anything under `%LOCALAPPDATA%\pyracast\` outside your own worktree. Do not start or
  stop the dev stack or PostgreSQL.
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
- Codex CLI 0.169.0: `gpt-6.1-sol` (routine review) and `gpt-6-astra` (critical
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
- CI takes about 17 minutes per run and the release about 8. The local time zone is
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
| 4. Implementation | in progress: tranche 1 (36 batches), wave 1. State per pull request is in the ledger | pull requests to `main`; `evaluation/LEDGER.md` |
| 5. Final scan (Qodana, Sonar) | pending | |

All paths in the Output column that start with `evaluation/` are under
`docs/agents/work/`.

## How one pull request runs

1. The batch has a readiness brief checked against `HEAD` (rule 13).
2. The orchestrator creates the worktree (`docs/agents/work/tools/new_worktree.ps1`).
3. An implementing agent commits the pins first (tests that fail for the stated reason,
   or characterisation tests), then the change with its documentation.
4. A reviewer that did not write the code reproduces red then green, and lists every
   changed assertion and new suppression. A Claude reviewer and Codex review the diff
   (tier A: the critical-decision models). Findings are verified before they are
   applied; an independent verifier closes the loop.
5. The orchestrator reads the diff, runs the full gate in the worktree, pushes the
   branch and opens the pull request. Green CI on both operating systems and the Sonar
   gate, then rebase-merge under the release-timing and stop rules (10 and 11).
6. The orchestrator records the row in the ledger, watches CI and the release on
   `main`, removes the worktree and branch, and fast-forwards the main checkout.

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

## Open questions for the owner

Explicit answers needed, none of them blocking wave 1:

- OD-31: scanner licence after the baseline scan (the trial ends 2026-12-12).
- OD-30, last part (with B08).
- OD-39 (copy of the working notes off this machine), OD-40 (disposable VM to rehearse
  installer changes), OD-08, OD-09, OD-27, OD-28, OD-29, OD-32.

## Other writers

`main` received commits from the owner while this program ran. Fetch before every
branch and before every merge; never overwrite upstream work.
