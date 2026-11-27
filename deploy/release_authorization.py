import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REQUIRED_CI_JOB = "CI result"
IMAGE_JOB = "Deployment configuration and containers"
MERGE_PROOF_JOB = "Find the pull request run that passed on this tree"
TESTED_TREE_JOB = "Record the tested tree"
SONAR_JOB = "Sonar quality gate"
STOP_JOB = "Stop the pull request run"
# A run of main skips these jobs when the merged pull request's run passed them on its tree.
PROVEN_JOBS = frozenset(
    {"Verify (ubuntu-latest)", "Web (ubuntu-latest)", "Scientific (ubuntu-latest)"}
)
PR_REQUIRED_JOBS = PROVEN_JOBS | {
    IMAGE_JOB,
    "Web production build (ubuntu-latest)",
    "Dependency audit",
    SONAR_JOB,
    REQUIRED_CI_JOB,
}
PROOF_HOURS = 24
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
FACTS_FILE = "release-facts.json"
PROOF_FILE = "merge-proof.json"
DEPLOYMENTS_FILE = "release-deployments.json"
RELEASED_EVENTS = frozenset({"push", "workflow_dispatch"})
# What the workflows execute or read to reach a verdict, beyond product and test code.
PIPELINE_DIRECTORIES = (".github/", "deploy/", "infra/", "scripts/", "web/eslint/", "web/testing/")
PIPELINE_FILES = frozenset(
    {
        ".dockerignore",
        ".editorconfig",
        ".gitattributes",
        ".gitignore",
        ".python-version",
        "Dockerfile",
        "pyproject.toml",
        "sonar-project.properties",
        "uv.lock",
        "web/.dockerignore",
        "web/.gitignore",
        "web/.prettierignore",
        "web/Dockerfile",
        "web/audit-exceptions.json",
        "web/bundle-budget.json",
        "web/coverage-floor.json",
        "web/eslint.config.mjs",
        "web/package-lock.json",
        "web/package.json",
        "web/playwright.config.ts",
        "web/playwright.integration.config.ts",
        "web/playwright.production.config.ts",
        "web/tests/support/server.ts",
        "web/tsconfig.json",
    }
)
SHA = re.compile(r"[0-9a-f]{40}")
IMAGE_ARTIFACT = re.compile(r"release-images-(?P<sha>[0-9a-f]{40})-(?P<attempt>[1-9][0-9]*)")
TESTED_TREE_ARTIFACT = re.compile(
    r"pr-tested-(?P<number>[1-9][0-9]*)-(?P<base>[0-9a-f]{40})-(?P<tree>[0-9a-f]{40})"
    r"-(?P<attempt>[1-9][0-9]*)"
)
GIT_TIMEOUT_SECONDS = 25


@dataclass(frozen=True)
class Proof:
    pull_request: int
    run: int


@dataclass(frozen=True)
class Authorized:
    sha: str
    ci_run_id: int
    ci_run_attempt: int
    artifact_id: int
    ci_started_at: str
    proof: Proof | None = None


@dataclass(frozen=True)
class Superseded:
    reason: str


class RefusedError(Exception):
    pass


class UnprovenError(Exception):
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


def changes_pipeline(path: str) -> bool:
    return path.startswith(PIPELINE_DIRECTORIES) or path in PIPELINE_FILES


def _newest_state(statuses: Sequence[Mapping[str, Any]]) -> object:
    # Statuses can share a one-second creation time; GitHub numbers them in creation order.
    if not statuses:
        return None
    newest = max(statuses, key=lambda status: (_instant(status["created_at"]), int(status["id"])))
    return newest["state"]


def _released_state(
    records: object, sha: str, since: object, states: Collection[str]
) -> bool | None:
    # None: the records or the start of the window are unreadable, so the answer is unknown.
    if not isinstance(records, list):
        return None
    try:
        start = _instant(since)
        return any(
            record["sha"] == sha
            and _instant(record["created_at"]) >= start
            and _newest_state(record["statuses"]) in states
            for record in records
        )
    except (KeyError, TypeError, ValueError):
        return None


def released_since(records: object, sha: str, since: object, states: Collection[str]) -> bool:
    return _released_state(records, sha, since, states) is True


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
    proof = _proof_of_skipped_jobs(facts, main, run)
    since = _release_baseline(run, facts["artifacts"], main)
    # Only a release that succeeded counts: one still running holds the serialized deploy job,
    # whose own check decides after it, and one that failed leaves this release to deploy.
    automatic = facts["event"] == "workflow_run"
    if automatic and released_since(facts.get("deployments"), main, since, {"success"}):
        return Superseded(f"{main} is already released from the images of CI run {run['id']}.")
    attempt, artifact = _newest_images(facts["artifacts"], main, int(run["run_attempt"]))
    if not _built_by_image_job(facts, run, artifact, attempt):
        raise RefusedError(_unbuilt_images(attempt))
    return Authorized(main, int(run["id"]), attempt, int(artifact["id"]), since, proof)


def _proof_of_skipped_jobs(
    facts: Mapping[str, Any], main: str, run: Mapping[str, Any]
) -> Proof | None:
    conclusions = [
        [job["conclusion"] for job in facts["jobs"] if job["name"] == name]
        for name in sorted(PROVEN_JOBS)
    ]
    if all(found == ["success"] for found in conclusions):
        return None
    if not all(found in (["success"], ["skipped"]) for found in conclusions):
        raise RefusedError(
            f"CI run {run['id']} did not end each of {', '.join(sorted(PROVEN_JOBS))} once"
        )
    if run["event"] != "push":
        raise RefusedError(f"Only a push to main may skip suites in CI run {run['id']}")
    try:
        return merge_proof(facts.get("proof"), facts.get("commit"), main)
    except UnprovenError as unproven:
        raise RefusedError(
            f"CI run {run['id']} skipped jobs that no pull request run proved: {unproven}"
        ) from None


def _release_baseline(
    run: Mapping[str, Any], artifacts: Sequence[Mapping[str, Any]], sha: str
) -> str:
    # An attempt that rebuilt its images is judged on its own; one that reuses an earlier
    # attempt's images (a re-run of failed jobs) is measured from the creation of the run.
    attempt = int(run["run_attempt"])
    rebuilt = any(
        (match := IMAGE_ARTIFACT.fullmatch(artifact["name"]))
        and match["sha"] == sha
        and int(match["attempt"]) == attempt
        for artifact in artifacts
    )
    return str(run.get("run_started_at" if rebuilt else "created_at") or "")


def merge_proof(proof: object, commit: object, main: str) -> Proof:
    if commit is None:
        raise UnprovenError(f"Git could not read the main commit {main}")
    if not isinstance(proof, Mapping) or "error" in proof:
        detail = proof.get("error") if isinstance(proof, Mapping) else None
        raise UnprovenError(f"The pull request facts are unavailable: {detail}")
    try:
        return _proof(proof, commit, main)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise UnprovenError(f"The pull request facts are malformed ({error!r})") from None


def _skippable(proof: Mapping[str, Any]) -> frozenset[str]:
    # Sonar skips only while off; the push-only job and an idle stop job always skip.
    if proof.get("sonar_enabled") is False:
        return frozenset({MERGE_PROOF_JOB, STOP_JOB, SONAR_JOB})
    return frozenset({MERGE_PROOF_JOB, STOP_JOB})


def _passed_every_job(jobs: Sequence[Mapping[str, Any]], skippable: Collection[str]) -> bool:
    names = [job["name"] for job in jobs]
    return all(names.count(name) == 1 for name in PR_REQUIRED_JOBS) and all(
        job["conclusion"] == "success"
        or (job["conclusion"] == "skipped" and job["name"] in skippable)
        for job in jobs
    )


def _proof(proof: Mapping[str, Any], commit: Any, main: str) -> Proof:
    if proof["sha"] != main:
        raise UnprovenError(f"The pull request facts name {proof['sha']}, not {main}")
    parents = list(commit["parents"])
    if len(parents) != 1:
        raise UnprovenError(f"The main commit {main} is not a squash merge")
    changed = [path for path in commit["paths"] if changes_pipeline(path)]
    if changed:
        raise UnprovenError(f"The main commit {main} changes the pipeline ({changed[0]})")
    pulls = [pull for pull in proof["pull_requests"] if pull["merge_commit_sha"] == main]
    if len(pulls) != 1:
        raise UnprovenError(
            f"The main commit {main} is not the squash merge of exactly one pull request"
        )
    (pull,) = pulls
    number = int(pull["number"])
    repository = proof["repository"]
    if (pull["base_ref"], pull["base_repository"], pull["head_repository"]) != (
        "main",
        repository,
        repository,
    ):
        raise UnprovenError(
            f"Pull request #{number} is not a branch of this repository merged into main"
        )
    tested: Mapping[str, Any] | None = proof["run"]
    if not (
        tested is not None
        and _repository_ci(tested, proof)
        and tested["event"] == "pull_request"
        and tested["head_sha"] == pull["head_sha"]
    ):
        raise UnprovenError(
            f"No CI run of pull request #{number} in this repository tested its final head "
            f"{pull['head_sha']}"
        )
    identifier = int(tested["id"])
    if (tested["status"], tested["conclusion"]) != ("completed", "success") or (
        not _passed_every_job(proof["jobs"], _skippable(proof))
    ):
        raise UnprovenError(f"CI run {identifier} of pull request #{number} did not pass every job")
    if not _merged_within(tested["created_at"], pull["merged_at"]):
        raise UnprovenError(
            f"Pull request #{number} was not merged within {PROOF_HOURS} hours of CI run "
            f"{identifier}"
        )
    attempt = int(tested["run_attempt"])
    evidence = [
        (match, artifact)
        for artifact in proof["artifacts"]
        if (match := TESTED_TREE_ARTIFACT.fullmatch(artifact["name"]))
        and int(match["attempt"]) == attempt
    ]
    if len(evidence) != 1:
        raise UnprovenError(
            f"CI run {identifier} recorded no single tested tree for attempt {attempt}"
        )
    ((match, artifact),) = evidence
    recorders = [job for job in proof["jobs"] if job["name"] == TESTED_TREE_JOB]
    tested_start = {attempt: tested.get("run_started_at")}
    if len(recorders) != 1 or not _made_by(artifact, recorders[0], attempt, tested_start):
        raise UnprovenError(
            f"The tested tree of CI run {identifier} was not recorded by its {TESTED_TREE_JOB} job"
        )
    if int(match["number"]) != number:
        raise UnprovenError(
            f"The tested tree of CI run {identifier} names pull request #{match['number']}"
        )
    if artifact["expired"] or artifact["head_sha"] != pull["head_sha"]:
        raise UnprovenError(
            f"The tested tree of CI run {identifier} has expired or belongs to another head"
        )
    if match["base"] != parents[0] or not _committed_before(
        commit["parent_committed_at"], tested["created_at"]
    ):
        raise UnprovenError(
            f"main moved after CI run {identifier}: it tested a merge onto {match['base']}, "
            f"the squash sits on {parents[0]}"
        )
    if match["tree"] != commit["tree"]:
        raise UnprovenError(
            f"CI run {identifier} tested tree {match['tree']}, but the main commit has tree "
            f"{commit['tree']}"
        )
    return Proof(number, identifier)


def _merged_within(created: object, merged: object) -> bool:
    # Tests that read the real clock are inherited only from a run of the same day.
    try:
        age = _instant(merged) - _instant(created)
    except (TypeError, ValueError):
        return False
    return timedelta(0) <= age <= timedelta(hours=PROOF_HOURS)


def _made_by(
    artifact: Mapping[str, Any],
    job: Mapping[str, Any],
    attempt: int,
    attempt_starts: Mapping[int, object],
) -> bool:
    try:
        started = _instant(job.get("started_at"))
        created = _instant(artifact.get("created_at"))
        ended = _instant(job.get("completed_at"))
        return _attempt_of(started, attempt_starts) == attempt and started <= created <= ended
    except (TypeError, ValueError):
        return False


def _attempt_of(started: datetime, attempt_starts: Mapping[int, object]) -> int | None:
    # A re-run lists the jobs it kept with the times of the attempt that ran them.
    begun = [attempt for attempt, start in attempt_starts.items() if _instant(start) <= started]
    return max(begun, default=None)


def _built_by_image_job(
    facts: Mapping[str, Any], run: Mapping[str, Any], artifact: Mapping[str, Any], attempt: int
) -> bool:
    builders = [job for job in facts["jobs"] if job["name"] == IMAGE_JOB]
    starts = _attempt_starts(facts, run)
    return (
        len(builders) == 1
        and starts is not None
        and _made_by(artifact, builders[0], attempt, starts)
    )


def _attempt_starts(facts: Mapping[str, Any], run: Mapping[str, Any]) -> dict[int, object] | None:
    current = int(run["run_attempt"])
    starts = {
        entry.get("attempt"): entry.get("run_started_at")
        for entry in facts["earlier_attempts"]
        if isinstance(entry.get("attempt"), int) and 1 <= entry["attempt"] < current
    }
    if len(starts) != current - 1:
        return None
    return starts | {current: run.get("run_started_at")}


def _unbuilt_images(attempt: int) -> str:
    return (
        f"The image artifact of attempt {attempt} was not created within the {IMAGE_JOB} job "
        "of that attempt"
    )


def _committed_before(committed: object, created: object) -> bool:
    try:
        return _instant(committed) <= _instant(created)
    except (TypeError, ValueError):
        return False


def _newest_images(
    artifacts: Sequence[Mapping[str, Any]], sha: str, run_attempt: int
) -> tuple[int, Mapping[str, Any]]:
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
    return attempt, newest[0]


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
    return result.stdout.decode("utf-8")


def commit_facts(root: Path, sha: str) -> dict[str, Any] | None:
    if SHA.fullmatch(sha) is None:
        return None
    try:
        tree = _git(root, "rev-parse", "--verify", f"{sha}^{{tree}}").strip()
        parents = _git(root, "rev-list", "--parents", "--max-count=1", sha).split()[1:]
        if len(parents) != 1:
            return {"tree": tree, "parents": parents, "paths": [], "parent_committed_at": None}
        changed = _git(root, "diff", "--name-only", "--no-renames", "-z", parents[0], sha)
        committed = _git(root, "log", "--max-count=1", "--format=%cI", parents[0]).strip()
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    paths = [path for path in changed.split("\0") if path]
    return {"tree": tree, "parents": parents, "paths": paths, "parent_committed_at": committed}


def _append(path: str | None, line: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")


def _authorize(facts_path: Path, proof_path: Path, environ: Mapping[str, str], root: Path) -> int:
    output = environ.get("GITHUB_OUTPUT")
    summary = environ.get("GITHUB_STEP_SUMMARY")
    try:
        facts = json.loads(facts_path.read_text(encoding="utf-8"))
        facts["commit"] = commit_facts(root, str(facts.get("main_sha")))
        facts["proof"] = _read_records(proof_path)
        decision = decide(facts)
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
        ("ci_started_at", decision.ci_started_at),
    ):
        _append(output, f"{key}={value}")
    message = f"Authorized main commit {decision.sha} using CI run {decision.ci_run_id}"
    if decision.proof is not None:
        message += (
            f"; CI run {decision.proof.run} of pull request #{decision.proof.pull_request} "
            "passed the jobs it skipped on the same tree"
        )
    _append(summary, f"{message}.")
    return 0


def _prove(proof_path: Path, environ: Mapping[str, str], root: Path) -> int:
    output = environ.get("GITHUB_OUTPUT")
    summary = environ.get("GITHUB_STEP_SUMMARY")
    main = environ.get("GITHUB_SHA", "")
    try:
        proof = merge_proof(_read_records(proof_path), commit_facts(root, main), main)
    except UnprovenError as unproven:
        _append(output, "proven=false")
        _append(summary, f"Every job runs: {unproven}.")
        return 0
    _append(output, "proven=true")
    _append(
        summary,
        f"CI run {proof.run} of pull request #{proof.pull_request} passed every job on this tree; "
        f"{', '.join(sorted(PROVEN_JOBS))} are not run again.",
    )
    return 0


def _runner_file(
    parser: argparse.ArgumentParser, runner_temp: str | None, requested: Path, name: str
) -> Path:
    if not runner_temp:
        parser.error("RUNNER_TEMP is required")
    expected = Path(runner_temp) / name
    if requested != expected:
        parser.error(f"only {expected} is read")
    return expected


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
    released = _released_state(_read_records(deployments), release, since, {"success"})
    if released:
        _append(output, "current=false")
        _append(
            summary,
            f"Already released: {release} was deployed after its CI run started; "
            "nothing is transferred.",
        )
        return 0
    _append(output, "current=true")
    if deployments is not None:
        _append(output, f"deployments={'unknown' if released is None else 'read'}")
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
    authorize.add_argument("proof", type=Path)
    prove = commands.add_parser("prove")
    prove.add_argument("proof", type=Path)
    current = commands.add_parser("current")
    current.add_argument("main_sha")
    current.add_argument("release_sha")
    current.add_argument("--deployments", type=Path)
    current.add_argument("--since")
    # An operator's dispatch looks up no deployments, so it reports no deployment state.
    current.add_argument("--deployment-lookup", choices=("true", "false"), default="true")
    options = parser.parse_args(arguments)
    runner_temp = environ.get("RUNNER_TEMP")
    if options.command == "authorize":
        facts = _runner_file(parser, runner_temp, options.facts, FACTS_FILE)
        proof = _runner_file(parser, runner_temp, options.proof, PROOF_FILE)
        return _authorize(facts, proof, environ, root or Path.cwd())
    if options.command == "prove":
        proof = _runner_file(parser, runner_temp, options.proof, PROOF_FILE)
        return _prove(proof, environ, root or Path.cwd())
    deployments = None
    if options.deployment_lookup == "true" and options.deployments is not None:
        deployments = _runner_file(parser, runner_temp, options.deployments, DEPLOYMENTS_FILE)
    return _current(options.main_sha, options.release_sha, environ, deployments, options.since)


if __name__ == "__main__":
    raise SystemExit(main())
