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
uv run --locked python tools/check.py
uv build --no-sources
```

`.python-version` and package metadata require CPython 3.15.2. `uv.lock` pins the
resolved stable development dependencies. Application dependencies will be added
with the first vertical slice, after their compatibility is checked. Use
`uv lock --upgrade` for deliberate upgrades, review the lock diff and rerun checks.
`uv run --locked ruff format .` formats code.

The check command runs formatting, Ruff, strict mypy and pytest with branch
coverage (85% minimum). Settings live in `pyproject.toml`. The initial test only
proves package installation/import; its coverage does not establish application
correctness. CI also builds and imports the wheel outside the checkout on Windows
and Ubuntu. These jobs verify implemented code, not the future database, provider
or queue stack. CI uses GitHub-hosted `ubuntu-latest` and `windows-latest` runners;
no self-hosted runner or organization-specific reusable workflow is required.

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

Start with [AGENTS.md](AGENTS.md). Repository skills in `.agents/skills/` adapt
the useful verification and testing practices from analysis-orchestrator.
Claude-specific rule frontmatter, permission hooks and unrelated service policies
are intentionally not copied. Local `.codex/` settings, IDE files, secrets and
scanner artifacts are ignored; this repository adds no execution approval hooks.
Agent execution permissions remain those granted by the installed client/session.

The local Sonar MCP connection is configured in ignored `.codex/config.toml`.
It uses the official Sonar MCP JAR and the installed Java runtime because Docker
is unavailable on this workstation. Its startup and tool discovery have been
verified. Restart the Codex session to load newly added MCP tools. Other machines
can use either Docker or Java 21+ following the
[official MCP setup](https://github.com/SonarSource/sonarqube-mcp-server).
