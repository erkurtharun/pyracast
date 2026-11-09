# Pyracast

Multi-tenant irradiance forecasting backend. Product scope and boundaries live in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); calculation contracts live in
[docs/IRRADIANCE.md](docs/IRRADIANCE.md). The repository currently contains the
development baseline, not a running API or worker.

## Development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run these
commands from the repository root in PowerShell or a Unix shell:

```text
uv sync --locked
uv run python scripts/check.py
uv build --no-sources
```

`.python-version` and package metadata require CPython 3.15.2. `uv.lock` pins the
resolved stable development dependencies. Application dependencies will be added
with the first vertical slice, after their compatibility is checked. Use
`uv lock --upgrade` for deliberate upgrades, review the lock diff and rerun checks.
`uv run --locked ruff format .` formats code.

The check command runs formatting, Ruff, strict mypy and parallel pytest with branch
coverage. The floor is 100%, measured on 2026-11-09; never lower it. Settings live in
`pyproject.toml`. The package smoke test only
proves package installation/import; its coverage does not establish application
correctness. CI also builds and imports the wheel outside the checkout on Windows
and Ubuntu. These jobs verify implemented code, not the future database, provider
or queue stack. CI uses GitHub-hosted `ubuntu-latest` and `windows-latest` runners;
no self-hosted runner or organization-specific reusable workflow is required.
CI forbids third-party source-distribution builds with `--no-build`; the first-party
project and its pinned build backend still build normally. A dependency without a
compatible wheel fails installation rather than executing an unexpected build script.

CI audits locked runtime and development dependencies against public vulnerability
advisories. To reproduce:

```text
uv export --locked --all-groups --no-emit-project --no-hashes --output-file audit-requirements.txt
uv run --locked pip-audit --strict --no-deps --disable-pip --requirement audit-requirements.txt
```

Dependabot proposes weekly uv and GitHub Actions updates; workflow actions are
pinned to immutable commits. No auto-merge or deployment is configured.

## SonarQube Cloud

The GitHub repository is connected to SonarQube Cloud project
[`erkurtharun_pyracast`](https://sonarcloud.io/project/overview?id=erkurtharun_pyracast)
in organization `erkurtharun`. CI-based analysis requires automatic analysis to be
disabled in the project's Administration > Analysis Method. GitHub repository settings use:

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `SONAR_TOKEN` | Token authorized to analyze this Sonar project |
| Variable | `SONAR_ORGANIZATION` | `erkurtharun` |
| Variable | `SONAR_PROJECT_KEY` | `erkurtharun_pyracast` |
| Variable | `SONAR_ENABLED` | `true`, after the values above are configured |

The `Sonar quality gate` job consumes Ubuntu's `coverage.xml`, fetches full Git
history and fails when the configured Sonar quality gate fails. Before enabling,
the job is explicitly skipped; local checks still run. Fork and Dependabot PRs
run verification without a Sonar token; merge-group events also skip analysis.
Do not use `pull_request_target` to run untrusted PR code with secrets.

The scanner target is honestly set to Python 3.15. Sonar's current
[Python support documentation](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/languages/python)
lists full support through 3.14, so complete analysis of 3.15 is unverified.
The token is stored as a GitHub Actions secret, never in tracked project files.
See the official [coverage configuration](https://docs.sonarsource.com/sonarqube-cloud/enriching/test-coverage/python-test-coverage)
and [scan action](https://github.com/SonarSource/sonarqube-scan-action).

## Agent workflow

Start with [AGENTS.md](AGENTS.md). Shared Codex lifecycle hooks live in
`.codex/hooks.json` and `.codex/hooks/quality.py`. They format and fix edited Python
files, run the check before stopping with dirty Python files, and avoid direct
source reads of environments, caches, build output and data. This is a tool-level
context guard, not a filesystem sandbox; shell commands and dependency loading still work.
Local `.codex/config.toml`, IDE state, secrets and scanner artifacts stay ignored.
Codex requires non-managed hooks to be reviewed and trusted through `/hooks` before
they run; the repository does not bypass that trust mechanism.

Install user-level CLI tools without registering MCP servers:

```text
npm install -g ctx7@0.10.0
uv tool install code-review-graph==2.3.9 --python 3.15.2
ctx7 --version
code-review-graph --version
```

Use `ctx7 library <name> <question>`, then `ctx7 docs <id> <question>` as documentation
leads. Do not run Context7 setup or the graph install/daemon/watch/embed/wiki commands.
The local Codex `[shell_environment_policy.set]` sets `CRG_HOME` to a per-user cache
outside the checkout (Windows: `%LOCALAPPDATA%/code-review-graph/pyracast`; Ubuntu:
choose a directory under `$XDG_CACHE_HOME` or `~/.cache`). Pass the same path to
`code-review-graph build --repo . --skip-flows --data-dir <CRG_HOME>` and subsequent
updates. Queries resolve this external database through the registry in `CRG_HOME`;
they do not support a `--data-dir` flag. No graph database belongs in git.

The local Sonar MCP connection is configured in ignored `.codex/config.toml`.
It uses the official Sonar MCP JAR and the installed Java runtime because Docker
is unavailable on this workstation. Its startup and tool discovery have been
verified. Restart the Codex session to load newly added MCP tools. Other machines
can use either Docker or Java 21+ following the
[official MCP setup](https://github.com/SonarSource/sonarqube-mcp-server).
