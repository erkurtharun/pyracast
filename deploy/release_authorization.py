import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REQUIRED_CI_JOB = "CI result"
IMAGE_JOB = "Deployment configuration and containers"
RELEASE_START_JOB = "Start the release of a proven merge"
# Jobs a passing pull-request run may skip: CI result judges Sonar, and the start runs on main only.
SKIPPABLE_PULL_REQUEST_JOBS = frozenset({"Sonar quality gate", RELEASE_START_JOB})
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
RELEASED_EVENTS = frozenset({"push", "workflow_dispatch"})
PIPELINE_PATHS = (".github/workflows/", "deploy/", "infra/")
SHA = re.compile(r"[0-9a-f]{40}")
IMAGE_ARTIFACT = re.compile(r"release-images-(?P<sha>[0-9a-f]{40})-(?P<attempt>[1-9][0-9]*)")
TESTED_TREE_ARTIFACT = re.compile(
    r"pr-tested-(?P<base>[0-9a-f]{40})-(?P<tree>[0-9a-f]{40})-(?P<attempt>[1-9][0-9]*)"
)
RELEASING_STATES = frozenset({"success", "in_progress", "queued", "pending"})
GIT_TIMEOUT_SECONDS = 25


@dataclass(frozen=True)
class Authorized:
    sha: str
    ci_run_id: int
    ci_run_attempt: int
    artifact_id: int
    ci_started_at: str
    pull_request: int | None = None
    pull_request_run: int | None = None


@dataclass(frozen=True)
class Superseded:
    reason: str


@dataclass(frozen=True)
class Deferred:
    reason: str


class RefusedError(Exception):
    pass


class _UnprovenError(Exception):
    pass


def _commit(value: object) -> str:
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise RefusedError(f"Unreadable main commit: {value!r}")
    return value


def _superseded(older: str, main: str) -> Superseded:
    return Superseded(f"main moved from {older} to {main}; the release for {main} deploys it.")


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("Expected an ISO 8601 time")
    return datetime.fromisoformat(value)


def released_since(records: object, sha: str, since: object, states: Collection[str]) -> bool:
    if not isinstance(records, list):
        return False
    try:
        start = _instant(since)
        return any(
            record["sha"] == sha
            and record["state"] in states
            and _instant(record["created_at"]) >= start
            for record in records
        )
    except (KeyError, TypeError, ValueError):
        return False


def _repository_ci(run: Mapping[str, Any] | None, facts: Mapping[str, Any]) -> bool:
    repository = facts["repository"]
    return (
        run is not None
        and run["workflow_id"] == facts["workflow_id"]
        and run["path"] == CI_WORKFLOW_PATH
        and run["repository"] == repository
        and run["head_repository"] == repository
    )


def _successful_repository_ci(run: Mapping[str, Any] | None, facts: Mapping[str, Any]) -> bool:
    return (
        run is not None
        and _repository_ci(run, facts)
        and run["event"] in RELEASED_EVENTS
        and run["head_branch"] == "main"
        and run["status"] == "completed"
        and run["conclusion"] == "success"
    )


def decide(facts: Mapping[str, Any]) -> Authorized | Superseded | Deferred:
    main = _commit(facts["main_sha"])
    run: Mapping[str, Any] | None = facts["run"]
    # The deployment record names the event's commit, so only that commit may be deployed.
    for candidate in (facts["event_sha"], run and run["head_sha"]):
        if candidate and candidate != main:
            return _superseded(str(candidate), main)
    if facts["event"] == "workflow_dispatch" and facts.get("requested_run") is not None:
        return _decide_early(facts, main, run)
    if run is None or not _successful_repository_ci(run, facts):
        raise RefusedError(
            "Only the current main SHA with successful repository CI may be released"
        )
    results = [job["conclusion"] for job in facts["jobs"] if job["name"] == REQUIRED_CI_JOB]
    if results != ["success"]:
        raise RefusedError(f"The {REQUIRED_CI_JOB} job of CI run {run['id']} did not pass")
    started = run.get("run_started_at")
    if facts["event"] == "workflow_run" and released_since(
        facts.get("deployments"), main, started, RELEASING_STATES
    ):
        return Superseded(
            f"{main} is already released, or being released, from the images of CI run "
            f"{run['id']} before its repeated jobs ended."
        )
    attempt, artifact = _newest_images(facts["artifacts"], main, int(run["run_attempt"]))
    return Authorized(main, int(run["id"]), attempt, artifact, str(started or ""))


def _decide_early(
    facts: Mapping[str, Any], main: str, run: Mapping[str, Any] | None
) -> Authorized | Deferred:
    # The release after the whole CI run decides whenever this start cannot prove its case.
    identifier = (run or facts["requested_run"])["id"]
    try:
        return _prove_early(facts, main, run)
    except _UnprovenError as unproven:
        reason = str(unproven)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        reason = f"The release facts are malformed ({error!r})"
    return Deferred(f"{reason}; the release after the whole CI run {identifier} decides.")


def _prove_early(facts: Mapping[str, Any], main: str, run: Mapping[str, Any] | None) -> Authorized:
    if run is None or not (
        _repository_ci(run, facts) and run["event"] == "push" and run["head_branch"] == "main"
    ):
        raise _UnprovenError("The requested run is not this repository's CI of a push to main")
    if run["status"] == "completed":
        raise _UnprovenError(f"CI run {run['id']} has ended")
    pull_request, pull_request_run = _proof(facts, main)
    built = [job["conclusion"] for job in facts["jobs"] if job["name"] == IMAGE_JOB]
    if built != ["success"]:
        raise _UnprovenError(f"The image job of CI run {run['id']} has not passed")
    attempt = int(run["run_attempt"])
    try:
        built_in, artifact = _newest_images(facts["artifacts"], main, attempt)
    except RefusedError:
        built_in, artifact = 0, 0
    if built_in != attempt:
        raise _UnprovenError(f"No unexpired image artifact of attempt {attempt} exists")
    return Authorized(
        main,
        int(run["id"]),
        attempt,
        artifact,
        str(run.get("run_started_at") or ""),
        pull_request,
        pull_request_run,
    )


def _passed_every_job(jobs: Sequence[Mapping[str, Any]]) -> bool:
    results = [job["conclusion"] for job in jobs if job["name"] == REQUIRED_CI_JOB]
    return results == ["success"] and all(
        job["conclusion"] == "success"
        or (job["conclusion"] == "skipped" and job["name"] in SKIPPABLE_PULL_REQUEST_JOBS)
        for job in jobs
    )


def _proof(facts: Mapping[str, Any], main: str) -> tuple[int, int]:
    commit = facts.get("commit")
    proof = facts.get("proof")
    if commit is None:
        raise _UnprovenError(f"Git could not read the main commit {main}")
    if not isinstance(proof, Mapping) or "error" in proof:
        detail = proof.get("error") if isinstance(proof, Mapping) else None
        raise _UnprovenError(f"The pull request facts are unavailable: {detail}")
    parents = list(commit["parents"])
    if len(parents) != 1:
        raise _UnprovenError(f"The main commit {main} is not a squash merge")
    changed = [path for path in commit["paths"] if path.startswith(PIPELINE_PATHS)]
    if changed:
        raise _UnprovenError(f"The main commit {main} changes the pipeline ({changed[0]})")
    pulls = [pull for pull in proof["pull_requests"] if pull["merge_commit_sha"] == main]
    if len(pulls) != 1:
        raise _UnprovenError(
            f"The main commit {main} is not the squash merge of exactly one pull request"
        )
    (pull,) = pulls
    number = int(pull["number"])
    repository = facts["repository"]
    if (pull["base_ref"], pull["base_repository"], pull["head_repository"]) != (
        "main",
        repository,
        repository,
    ):
        raise _UnprovenError(
            f"Pull request #{number} is not a branch of this repository merged into main"
        )
    tested: Mapping[str, Any] | None = proof["run"]
    if not (
        tested is not None
        and _repository_ci(tested, facts)
        and tested["event"] == "pull_request"
        and tested["head_sha"] == pull["head_sha"]
    ):
        raise _UnprovenError(
            f"No CI run of pull request #{number} in this repository tested its final head "
            f"{pull['head_sha']}"
        )
    identifier = int(tested["id"])
    if (tested["status"], tested["conclusion"]) != ("completed", "success") or (
        not _passed_every_job(proof["jobs"])
    ):
        raise _UnprovenError(
            f"CI run {identifier} of pull request #{number} did not pass every job"
        )
    attempt = int(tested["run_attempt"])
    evidence = [
        (match, artifact)
        for artifact in proof["artifacts"]
        if (match := TESTED_TREE_ARTIFACT.fullmatch(artifact["name"]))
        and int(match["attempt"]) == attempt
    ]
    if len(evidence) != 1:
        raise _UnprovenError(
            f"CI run {identifier} recorded no single tested tree for attempt {attempt}"
        )
    ((match, artifact),) = evidence
    if artifact["expired"] or artifact["head_sha"] != pull["head_sha"]:
        raise _UnprovenError(
            f"The tested tree of CI run {identifier} has expired or belongs to another head"
        )
    if match["base"] != parents[0] or not _committed_before(
        commit["parent_committed_at"], tested["created_at"]
    ):
        raise _UnprovenError(
            f"main moved after CI run {identifier}: it tested a merge onto {match['base']}, "
            f"the squash sits on {parents[0]}"
        )
    if match["tree"] != commit["tree"]:
        raise _UnprovenError(
            f"CI run {identifier} tested tree {match['tree']}, but the main commit has tree "
            f"{commit['tree']}"
        )
    return number, identifier


def _committed_before(committed: object, created: object) -> bool:
    try:
        return _instant(committed) <= _instant(created)
    except (TypeError, ValueError):
        return False


def _newest_images(
    artifacts: Sequence[Mapping[str, Any]], sha: str, run_attempt: int
) -> tuple[int, int]:
    # The newest build up to the authorizing attempt: a partial re-run keeps earlier images, and a
    # later attempt never changes what this release deploys.
    built = [
        (int(match["attempt"]), artifact)
        for artifact in artifacts
        if (match := IMAGE_ARTIFACT.fullmatch(artifact["name"])) and match["sha"] == sha
    ]
    attempt = max((built_in for built_in, _ in built if built_in <= run_attempt), default=0)
    newest = [artifact for built_in, artifact in built if built_in == attempt]
    if len(newest) != 1 or newest[0]["expired"] or newest[0]["head_sha"] != sha:
        raise RefusedError("Run CI again to produce an unexpired image artifact")
    return attempt, int(newest[0]["id"])


def cleanup_order(artifacts: Sequence[Mapping[str, Any]], sha: str, run_attempt: int) -> list[int]:
    # Mirrors the cleanup job of release.yml: every unexpired build up to the released attempt,
    # oldest first, so a partly failed cleanup never leaves an older build without the selected one.
    built = sorted(
        (int(match["attempt"]), int(artifact["id"]))
        for artifact in artifacts
        if (match := IMAGE_ARTIFACT.fullmatch(artifact["name"]))
        and match["sha"] == sha
        and int(match["attempt"]) <= run_attempt
        and not artifact["expired"]
    )
    return [identifier for _, identifier in built]


def _git(root: Path, *arguments: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("git")
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    result = subprocess.run(  # noqa: S603 -- Resolved git executable; arguments are validated SHAs.
        [git, "-C", str(root), *arguments],
        capture_output=True,
        check=False,
        timeout=GIT_TIMEOUT_SECONDS,
        env=environment,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout)
    return result.stdout.decode("utf-8").strip()


def commit_facts(root: Path, sha: str) -> dict[str, Any] | None:
    if SHA.fullmatch(sha) is None:
        return None
    try:
        tree = _git(root, "rev-parse", "--verify", f"{sha}^{{tree}}")
        parents = _git(root, "rev-list", "--parents", "--max-count=1", sha).split()[1:]
        if len(parents) != 1:
            return {"tree": tree, "parents": parents, "paths": [], "parent_committed_at": None}
        changed = _git(root, "diff", "--name-only", "--no-renames", "-z", parents[0], sha)
        committed = _git(root, "log", "--max-count=1", "--format=%cI", parents[0])
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    paths = [path for path in changed.split("\0") if path]
    return {"tree": tree, "parents": parents, "paths": paths, "parent_committed_at": committed}


def _append(path: str | None, line: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")


def _authorize(facts_path: Path, environ: Mapping[str, str], root: Path) -> int:
    output = environ.get("GITHUB_OUTPUT")
    summary = environ.get("GITHUB_STEP_SUMMARY")
    try:
        facts = json.loads(facts_path.read_text(encoding="utf-8"))
        if facts.get("requested_run") is not None:
            facts["commit"] = commit_facts(root, str(facts.get("main_sha")))
        decision = decide(facts)
    except RefusedError as refused:
        _append(output, "authorized=false")
        _append(summary, f"Refused: {refused}")
        print(f"Refused: {refused}", file=sys.stderr)
        return 1
    if isinstance(decision, Superseded | Deferred):
        _append(output, "authorized=false")
        _append(summary, f"{type(decision).__name__}: {decision.reason}")
        return 0
    for key, value in (
        ("authorized", "true"),
        ("sha", decision.sha),
        ("ci_run_id", decision.ci_run_id),
        ("ci_run_attempt", decision.ci_run_attempt),
        ("artifact_id", decision.artifact_id),
        ("ci_started_at", decision.ci_started_at),
        ("proven", str(decision.pull_request is not None).lower()),
    ):
        _append(output, f"{key}={value}")
    message = f"Authorized main commit {decision.sha} using CI run {decision.ci_run_id}"
    if decision.pull_request is not None:
        message += (
            f" before its repeated jobs ended: CI run {decision.pull_request_run} of pull request "
            f"#{decision.pull_request} passed on the same tree"
        )
    _append(summary, f"{message}.")
    return 0


def _read_records(path: Path | None) -> object:
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _current(
    main_sha: str,
    release_sha: str,
    environ: Mapping[str, str],
    deployments: Path | None,
    since: str | None,
) -> int:
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
    # Deploy jobs are serialized, so a success recorded here belongs to another, finished release.
    if released_since(_read_records(deployments), release, since, {"success"}):
        _append(output, "current=false")
        _append(
            summary,
            f"Already released: {release} was deployed after its CI run started; "
            "nothing is transferred.",
        )
        return 0
    _append(output, "current=true")
    return 0


def main(
    arguments: Sequence[str] | None = None,
    environ: Mapping[str, str] = os.environ,
    root: Path | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    authorize = commands.add_parser("authorize")
    authorize.add_argument("facts", type=Path)
    current = commands.add_parser("current")
    current.add_argument("main_sha")
    current.add_argument("release_sha")
    current.add_argument("--deployments", type=Path)
    current.add_argument("--since")
    options = parser.parse_args(arguments)
    if options.command == "authorize":
        return _authorize(options.facts, environ, root or Path.cwd())
    return _current(
        options.main_sha, options.release_sha, environ, options.deployments, options.since
    )


if __name__ == "__main__":
    raise SystemExit(main())
