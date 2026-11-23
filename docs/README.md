# Documentation index

Read on demand. The code is the source of truth; these pages hold what it cannot show at a glance.

| Need | Read |
| --- | --- |
| What the system is built from, at which versions, and the external rules that bind each part | [STACK.md](STACK.md) |
| How one user, operator or background flow works end to end | [flows/README.md](flows/README.md), then the page of that flow |
| Tables, columns, relations and constraints (generated, do not edit) | [DATA_MODEL.md](DATA_MODEL.md) |
| System-wide design decisions and their rationale | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Irradiance, energy and calibration calculations | [IRRADIANCE.md](IRRADIANCE.md) |
| Production topology, release and recovery | [DEPLOYMENT.md](DEPLOYMENT.md) |
| How contributors and coding agents work, verify and deliver | [AGENTS.md](../AGENTS.md) |

## Keeping it true

- A change to a flow updates that flow's page in the same commit.
- A change to models or migrations regenerates the data model with
  `uv run python scripts/generate_er_diagram.py`; the quality gate fails on drift.
- A change to a pinned version updates its row in `STACK.md`.
- Where `ARCHITECTURE.md` and a flow page disagree, the flow page and the code win.
