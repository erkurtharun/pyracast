import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUIRED_CI_JOB = "CI result"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
RELEASED_EVENTS = frozenset({"push", "workflow_dispatch"})
SHA = re.compile(r"[0-9a-f]{40}")
IMAGE_ARTIFACT = re.compile(r"release-images-(?P<sha>[0-9a-f]{40})-(?P<attempt>[1-9][0-9]*)")


@dataclass(frozen=True)
class Authorized:
    sha: str
    ci_run_id: int
    ci_run_attempt: int
    artifact_id: int


@dataclass(frozen=True)
class Superseded:
    reason: str


class RefusedError(Exception):
    pass


def _commit(value: object) -> str:
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise RefusedError(f"Unreadable main commit: {value!r}")
    return value


def _superseded(older: str, main: str) -> Superseded:
    return Superseded(f"main moved from {older} to {main}; the release for {main} deploys it.")


def _successful_repository_ci(run: Mapping[str, Any] | None, facts: Mapping[str, Any]) -> bool:
    repository = facts["repository"]
    return (
        run is not None
        and run["workflow_id"] == facts["workflow_id"]
        and run["path"] == CI_WORKFLOW_PATH
        and run["repository"] == repository
        and run["head_repository"] == repository
        and run["event"] in RELEASED_EVENTS
        and run["head_branch"] == "main"
        and run["status"] == "completed"
        and run["conclusion"] == "success"
    )


def decide(facts: Mapping[str, Any]) -> Authorized | Superseded:
    main = _commit(facts["main_sha"])
    run: Mapping[str, Any] | None = facts["run"]
    # The deployment record names the event's commit, so only that commit may be deployed.
    for candidate in (facts["event_sha"], run and run["head_sha"]):
        if candidate and candidate != main:
            return _superseded(str(candidate), main)
    if run is None or not _successful_repository_ci(run, facts):
        raise RefusedError(
            "Only the current main SHA with successful repository CI may be released"
        )
    results = [job["conclusion"] for job in facts["jobs"] if job["name"] == REQUIRED_CI_JOB]
    if results != ["success"]:
        raise RefusedError(f"The {REQUIRED_CI_JOB} job of CI run {run['id']} did not pass")
    attempt, artifact = _newest_images(facts["artifacts"], main, int(run["run_attempt"]))
    return Authorized(main, int(run["id"]), attempt, artifact)


def _newest_images(
    artifacts: Sequence[Mapping[str, Any]], sha: str, run_attempt: int
) -> tuple[int, int]:
    # A partial re-run keeps the images of the attempt that built them; a rebuild replaces them.
    built = [
        (int(match["attempt"]), artifact)
        for artifact in artifacts
        if (match := IMAGE_ARTIFACT.fullmatch(artifact["name"])) and match["sha"] == sha
    ]
    attempt = max((built_in for built_in, _ in built), default=0)
    newest = [artifact for built_in, artifact in built if built_in == attempt]
    if (
        not 1 <= attempt <= run_attempt
        or len(newest) != 1
        or newest[0]["expired"]
        or newest[0]["head_sha"] != sha
    ):
        raise RefusedError("Run CI again to produce an unexpired image artifact")
    return attempt, int(newest[0]["id"])


def _append(path: str | None, line: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")


def _authorize(facts_path: Path, environ: Mapping[str, str]) -> int:
    output = environ.get("GITHUB_OUTPUT")
    summary = environ.get("GITHUB_STEP_SUMMARY")
    try:
        decision = decide(json.loads(facts_path.read_text(encoding="utf-8")))
    except RefusedError as refused:
        _append(output, "authorized=false")
        _append(summary, f"Refused: {refused}")
        print(f"Refused: {refused}", file=sys.stderr)
        return 1
    if isinstance(decision, Superseded):
        _append(output, "authorized=false")
        _append(summary, f"Superseded: {decision.reason}")
        return 0
    for key, value in (
        ("authorized", "true"),
        ("sha", decision.sha),
        ("ci_run_id", decision.ci_run_id),
        ("ci_run_attempt", decision.ci_run_attempt),
        ("artifact_id", decision.artifact_id),
    ):
        _append(output, f"{key}={value}")
    _append(summary, f"Authorized main commit {decision.sha} using CI run {decision.ci_run_id}.")
    return 0


def _current(main_sha: str, release_sha: str, environ: Mapping[str, str]) -> int:
    output = environ.get("GITHUB_OUTPUT")
    summary = environ.get("GITHUB_STEP_SUMMARY")
    try:
        main = _commit(main_sha)
        release = _commit(release_sha)
    except RefusedError as refused:
        _append(output, "current=false")
        _append(summary, f"Refused: {refused}")
        return 1
    if main != release:
        _append(output, "current=false")
        _append(summary, f"Superseded: {_superseded(release, main).reason}")
        return 0
    _append(output, "current=true")
    return 0


def main(arguments: Sequence[str] | None = None, environ: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    authorize = commands.add_parser("authorize")
    authorize.add_argument("facts", type=Path)
    current = commands.add_parser("current")
    current.add_argument("main_sha")
    current.add_argument("release_sha")
    options = parser.parse_args(arguments)
    if options.command == "authorize":
        return _authorize(options.facts, environ)
    return _current(options.main_sha, options.release_sha, environ)


if __name__ == "__main__":
    raise SystemExit(main())
