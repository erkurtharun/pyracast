import json
import os
from collections.abc import Mapping
from typing import Any

# Sonar is skipped when its condition is false or a job it needs did not run; such a job is judged
# on its own.
SKIPPABLE = frozenset({"sonar"})
PROOF_JOB = "merge_proof"
# The jobs a push skips when its first job found the merged pull request's run of the same tree.
PROVEN_JOBS = frozenset({"verify_ubuntu", "web_ubuntu", "scientific"})


def proven(needs: Mapping[str, Mapping[str, Any]]) -> bool:
    proof = needs.get(PROOF_JOB, {})
    outputs = proof.get("outputs")
    return (
        proof.get("result") == "success"
        and isinstance(outputs, Mapping)
        and outputs.get("proven") == "true"
    )


def skippable(needs: Mapping[str, Mapping[str, Any]], event: str) -> frozenset[str]:
    if event != "push":
        return SKIPPABLE | {PROOF_JOB}
    if proven(needs):
        return SKIPPABLE | PROVEN_JOBS
    return SKIPPABLE


def failures(needs: Mapping[str, Mapping[str, Any]], event: str) -> list[str]:
    allowed = skippable(needs, event)
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
    # A skipped job reports success to a required check, so a draft's run must fail here.
    if environ.get("PULL_REQUEST_DRAFT") == "true":
        print("A draft pull request is not verified; mark it ready for review to run CI.")
        return 1
    event = environ.get("GITHUB_EVENT_NAME", "")
    failed = failures(needs, event)
    if failed:
        print(f"These CI jobs did not pass: {', '.join(failed)}.")
        return 1
    if any(needs.get(name, {}).get("result") == "skipped" for name in PROVEN_JOBS):
        print(f"All {len(needs)} CI jobs passed or were passed on this tree by the pull request.")
        return 0
    print(f"All {len(needs)} CI jobs passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
