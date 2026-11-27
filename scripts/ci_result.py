import json
import os
from collections.abc import Mapping
from typing import Any

PLAN = "plan"
AUDIT = "audit"
# Sonar is skipped when its condition is false or a job it needs did not pass; such a job fails
# the aggregate on its own.
SONAR = "sonar"


def skippable(event: str, audit_owed: bool) -> frozenset[str]:
    if event == "pull_request" and not audit_owed:
        return frozenset({SONAR, AUDIT})
    return frozenset({SONAR})


def planned_audit(needs: Mapping[str, Mapping[str, Any]]) -> bool:
    plan = needs.get(PLAN, {})
    outputs = plan.get("outputs") or {}
    return plan.get("result") != "success" or outputs.get("audit") != "false"


def failures(needs: Mapping[str, Mapping[str, Any]], event: str) -> list[str]:
    allowed = skippable(event, planned_audit(needs))
    return sorted(
        name
        for name, need in needs.items()
        if need["result"] != "success" and not (need["result"] == "skipped" and name in allowed)
    )


def read_needs(value: str) -> dict[str, dict[str, Any]]:
    try:
        needs = json.loads(value)
    except ValueError:
        raise ValueError("The CI needs are not JSON.") from None
    if (
        not isinstance(needs, dict)
        or not needs
        or not all(
            isinstance(need, dict) and isinstance(need.get("result"), str)
            for need in needs.values()
        )
    ):
        raise ValueError("The CI needs name no job results.")
    return needs


def main(environ: Mapping[str, str] = os.environ) -> int:
    needs = read_needs(environ["NEEDS"])
    failed = failures(needs, environ["GITHUB_EVENT_NAME"])
    if failed:
        print(f"These CI jobs did not pass: {', '.join(failed)}.")
        return 1
    print(f"All {len(needs)} CI jobs passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
