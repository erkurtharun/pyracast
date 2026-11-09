# Start prompt

Select your GPT-6.0 Ultra orchestrator profile in Codex, open the project root, and paste the text below. Keep `AGENTS.md` at the project root and the other files in `docs/`. If the project already has an `AGENTS.md`, merge the relevant instructions instead of replacing unrelated rules.

```text
Build the backend described in docs/ARCHITECTURE.md and docs/IRRADIANCE.md. Follow the root AGENTS.md.

Inspect the existing project, verify the latest stable dependencies against Python 3.15.2 and Windows/Ubuntu, then implement working vertical slices. Use the model and effort allocation in AGENTS.md, delegating independent work selectively to GPT-6.1 Sol and Terra to keep context and token cost low.

Use current, idiomatic engineering practices. Research uncertain facts in primary documentation and choose a materially better approach when supported by evidence, while preserving the product and calculation contracts. Keep code expressive, comments exceptional and documentation minimal.

Continue through implementation and relevant checks. Use fixtures when external credentials or reference data are unavailable; never present mocked integration or unvalidated irradiance as verified. Resolve routine choices autonomously and raise only consequential ambiguity or a real blocker.

Finish with a concise Turkish summary of what works, how to run it, checks performed and remaining limitations. Do not stop after proposing a plan.
```
