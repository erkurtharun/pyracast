import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from deploy import release_authorization
from deploy.release_authorization import (
    Authorized,
    Deferred,
    RefusedError,
    Superseded,
    commit_facts,
    decide,
    released_since,
)

MAIN = "a" * 40
OLDER = "b" * 40
PARENT = "c" * 40
TREE = "d" * 40
HEAD = "e" * 40
OTHER = "f" * 40
REPOSITORY = "owner/pyracast"
FORK = "fork/pyracast"
WORKFLOW = 41
STARTED = "2026-11-21T05:00:00Z"
BEFORE_RUN = "2026-11-21T04:00:00Z"
AFTER_START = "2026-11-21T05:10:00Z"
PULL_RUN_CREATED = "2026-11-21T04:30:00Z"
GIT = shutil.which("git")


def facts(run_changes: dict[str, Any] | None = None, **changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "event": "workflow_run",
        "event_sha": MAIN,
        "main_sha": MAIN,
        "repository": REPOSITORY,
        "workflow_id": WORKFLOW,
        "requested_run": None,
        "run": {
            "id": 7001,
            "run_attempt": 2,
            "workflow_id": WORKFLOW,
            "path": ".github/workflows/ci.yml",
            "repository": REPOSITORY,
            "head_repository": REPOSITORY,
            "event": "push",
            "head_branch": "main",
            "head_sha": MAIN,
            "status": "completed",
            "conclusion": "success",
            "created_at": STARTED,
            "run_started_at": STARTED,
            **(run_changes or {}),
        },
        "jobs": [
            {"name": "Verify (ubuntu-latest)", "conclusion": "success"},
            {"name": "CI result", "conclusion": "success"},
        ],
        "artifacts": [
            {"id": 91, "name": f"release-images-{MAIN}-2", "expired": False, "head_sha": MAIN},
            {"id": 92, "name": "coverage-2", "expired": False, "head_sha": MAIN},
        ],
        "deployments": [],
    }
    value.update(changes)
    return value


def authorized(**changes: Any) -> Authorized:
    value: dict[str, Any] = {
        "sha": MAIN,
        "ci_run_id": 7001,
        "ci_run_attempt": 2,
        "artifact_id": 91,
        "ci_started_at": STARTED,
    }
    value.update(changes)
    return Authorized(**value)


@pytest.mark.parametrize("event", ["workflow_run", "workflow_dispatch"])
@pytest.mark.parametrize("run_event", ["push", "workflow_dispatch"])
def test_the_current_main_commit_with_a_passing_ci_result_is_authorized(
    event: str, run_event: str
) -> None:
    assert decide(facts({"event": run_event}, event=event)) == authorized()


@pytest.mark.parametrize(
    ("run", "changes"),
    [
        ({}, {"event_sha": OLDER}),
        ({"head_sha": OLDER}, {}),
        ({}, {"event_sha": OLDER, "run": None}),
        ({"conclusion": "failure"}, {"event_sha": OLDER}),
    ],
)
def test_a_run_for_a_commit_that_is_no_longer_main_is_superseded(
    run: dict[str, Any], changes: dict[str, Any]
) -> None:
    decision = decide(facts(run, **changes))

    assert isinstance(decision, Superseded)
    assert MAIN in decision.reason


@pytest.mark.parametrize(
    ("run", "changes"),
    [
        ({}, {"run": None}),
        ({"workflow_id": 42}, {}),
        ({"path": ".github/workflows/release.yml"}, {}),
        ({"repository": "fork/pyracast"}, {}),
        ({"head_repository": "fork/pyracast"}, {}),
        ({"head_repository": None}, {}),
        ({"event": "pull_request"}, {}),
        ({"event": "schedule"}, {}),
        ({"head_branch": "feature"}, {}),
        ({"status": "in_progress"}, {}),
        ({"conclusion": "failure"}, {}),
        ({"conclusion": None}, {}),
    ],
)
def test_a_run_that_is_not_successful_repository_ci_on_main_is_refused(
    run: dict[str, Any], changes: dict[str, Any]
) -> None:
    with pytest.raises(RefusedError, match="successful repository CI"):
        decide(facts(run, **changes))


@pytest.mark.parametrize(
    "jobs",
    [
        [],
        [{"name": "Verify (ubuntu-latest)", "conclusion": "success"}],
        [{"name": "CI result", "conclusion": "failure"}],
        [{"name": "CI result", "conclusion": "skipped"}],
        [
            {"name": "CI result", "conclusion": "success"},
            {"name": "CI result", "conclusion": "failure"},
        ],
    ],
)
def test_only_a_single_passing_ci_result_job_authorizes(jobs: list[dict[str, str]]) -> None:
    with pytest.raises(RefusedError, match="CI result"):
        decide(facts(jobs=jobs))


def images(attempt: int, artifact: int = 91, **changes: Any) -> dict[str, Any]:
    name = f"release-images-{MAIN}-{attempt}"
    return {"id": artifact, "name": name, "expired": False, "head_sha": MAIN, **changes}


def test_a_partial_rerun_releases_the_images_of_the_attempt_that_built_them() -> None:
    assert decide(facts(artifacts=[images(1, 90)])) == authorized(ci_run_attempt=1, artifact_id=90)


def test_images_rebuilt_in_a_later_attempt_replace_the_earlier_ones() -> None:
    assert decide(facts(artifacts=[images(1, 91), images(2, 90)])) == authorized(artifact_id=90)


def test_a_later_ci_attempt_leaves_the_images_of_the_authorizing_attempt_in_place() -> None:
    artifacts = [images(1, 90), images(2, 91)]

    assert decide(facts({"run_attempt": 1}, artifacts=artifacts)) == authorized(
        ci_run_attempt=1, artifact_id=90
    )


def test_a_release_rerun_after_its_cleanup_finds_no_earlier_build_to_deploy() -> None:
    artifacts = [images(1, 90), images(2, 91), {**images(1, 89), "name": "coverage-1"}]
    released = decide(facts(artifacts=artifacts))
    assert isinstance(released, Authorized)

    removed = release_authorization.cleanup_order(artifacts, released.sha, released.ci_run_attempt)
    remaining = [artifact for artifact in artifacts if artifact["id"] not in removed]

    with pytest.raises(RefusedError, match="Run CI again"):
        decide(facts(artifacts=remaining))
    assert remaining == [artifacts[2]]


def test_the_cleanup_keeps_later_attempts_and_other_commits_and_removes_the_selected_last() -> None:
    artifacts = [
        images(2, 91),
        images(3, 93),
        images(1, 90),
        images(1, 88, expired=True),
        {**images(1, 87), "name": f"release-images-{OLDER}-1"},
    ]

    assert release_authorization.cleanup_order(artifacts, MAIN, 2) == [90, 91]


@pytest.mark.parametrize(
    "artifacts",
    [
        [],
        [images(2, expired=True)],
        [images(1, 90), images(2, expired=True)],
        [images(2, head_sha=OLDER)],
        [images(1, 90), images(2, head_sha=OLDER)],
        [images(2), images(2, 93)],
        [images(3)],
        [{**images(2), "name": f"release-images-{OLDER}-2"}],
        [{**images(2), "name": f"release-images-{MAIN}-0"}],
    ],
)
def test_the_newest_image_build_must_be_one_unexpired_artifact_of_the_commit(
    artifacts: list[dict[str, Any]],
) -> None:
    with pytest.raises(RefusedError, match="unexpired image artifact"):
        decide(facts(artifacts=artifacts))


@pytest.mark.parametrize("main", ["", "A" * 40, "a" * 39, None])
def test_an_unreadable_main_commit_is_refused(main: str | None) -> None:
    with pytest.raises(RefusedError, match="main commit"):
        decide(facts({"head_sha": main}, main_sha=main, event_sha=main))


def deployment(**changes: Any) -> dict[str, Any]:
    return {"sha": MAIN, "created_at": AFTER_START, "state": "success", **changes}


@pytest.mark.parametrize("state", ["success", "in_progress", "queued", "pending"])
def test_the_release_after_the_whole_run_skips_a_commit_released_since_the_run_started(
    state: str,
) -> None:
    decision = decide(facts(deployments=[deployment(state=state)]))

    assert isinstance(decision, Superseded)
    assert f"{MAIN} is already released" in decision.reason


@pytest.mark.parametrize(
    "deployments",
    [
        None,
        [deployment(state="failure")],
        [deployment(state="error")],
        [deployment(state="inactive")],
        [deployment(created_at=BEFORE_RUN)],
        [deployment(sha=OLDER)],
        [deployment(created_at="yesterday")],
        [{"sha": MAIN}],
        "unreadable",
    ],
)
def test_the_release_after_the_whole_run_deploys_without_an_earlier_release_of_this_run(
    deployments: Any,
) -> None:
    assert decide(facts(deployments=deployments)) == authorized()


def test_an_operator_dispatch_releases_a_commit_already_released_from_its_run() -> None:
    dispatched = facts(event="workflow_dispatch", deployments=[deployment()])

    assert decide(dispatched) == authorized()


def test_the_released_check_needs_a_readable_start_of_the_ci_run() -> None:
    assert not released_since([deployment()], MAIN, None, {"success"})
    assert not released_since([deployment()], MAIN, "", {"success"})
    assert released_since([deployment()], MAIN, STARTED, {"success"})


IMAGE_JOB = "Deployment configuration and containers"
PULL_REQUEST_JOBS = [
    IMAGE_JOB,
    "Verify (ubuntu-latest)",
    "Windows tests (1/4)",
    "Verify (windows-latest)",
    "Web (ubuntu-latest)",
    "Scientific (ubuntu-latest)",
    "Dependency audit",
    "Sonar quality gate",
    "CI result",
]


def early(
    *,
    run: dict[str, Any] | None = None,
    commit: dict[str, Any] | None = None,
    pull: dict[str, Any] | None = None,
    tested: dict[str, Any] | None = None,
    tested_jobs: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    **changes: Any,
) -> dict[str, Any]:
    value = facts(
        {"status": "in_progress", "conclusion": None, "run_attempt": 1, **(run or {})},
        event="workflow_dispatch",
        requested_run={"id": 7001, "attempt": 1},
        jobs=[
            {"name": IMAGE_JOB, "conclusion": "success"},
            {"name": "Verify (ubuntu-latest)", "conclusion": None},
            {"name": "CI result", "conclusion": None},
        ],
        artifacts=[images(1, 91)],
    )
    value["commit"] = {
        "tree": TREE,
        "parents": [PARENT],
        "paths": ["src/pyracast/views.py", "docs/DEPLOYMENT.md"],
        "parent_committed_at": BEFORE_RUN,
        **(commit or {}),
    }
    value["proof"] = {
        "pull_requests": [
            {
                "number": 82,
                "merge_commit_sha": MAIN,
                "base_ref": "main",
                "base_repository": REPOSITORY,
                "head_repository": REPOSITORY,
                "head_sha": HEAD,
                **(pull or {}),
            }
        ],
        "run": {
            "id": 6001,
            "run_attempt": 1,
            "workflow_id": WORKFLOW,
            "path": ".github/workflows/ci.yml",
            "repository": REPOSITORY,
            "head_repository": REPOSITORY,
            "event": "pull_request",
            "head_branch": "feature",
            "head_sha": HEAD,
            "status": "completed",
            "conclusion": "success",
            "created_at": PULL_RUN_CREATED,
            "run_started_at": PULL_RUN_CREATED,
            **(tested or {}),
        },
        "jobs": (
            tested_jobs
            if tested_jobs is not None
            else [{"name": name, "conclusion": "success"} for name in PULL_REQUEST_JOBS]
        ),
        "artifacts": (
            evidence
            if evidence is not None
            else [
                {
                    "id": 51,
                    "name": "pytest-results-Linux-full-1",
                    "expired": False,
                    "head_sha": HEAD,
                },
                evidence_artifact(),
            ]
        ),
    }
    value.update(changes)
    return value


def evidence_artifact(
    base: str = PARENT, tree: str = TREE, attempt: int = 1, **changes: Any
) -> dict[str, Any]:
    name = f"pr-tested-{base}-{tree}-{attempt}"
    return {"id": 52, "name": name, "expired": False, "head_sha": HEAD, **changes}


def proven() -> Authorized:
    return authorized(ci_run_attempt=1, pull_request=82, pull_request_run=6001)


def deferred(value: dict[str, Any], reason: str) -> None:
    decision = decide(value)

    assert isinstance(decision, Deferred), decision
    assert reason in decision.reason, decision.reason


def test_a_squash_merge_whose_pull_request_run_tested_the_same_tree_is_released_early() -> None:
    assert decide(early()) == proven()


def jobs_with(name: str, conclusion: str | None) -> list[dict[str, Any]]:
    return [
        {"name": job, "conclusion": conclusion if job == name else "success"}
        for job in PULL_REQUEST_JOBS
    ]


def test_the_pull_request_run_may_skip_sonar_and_the_release_start_alone() -> None:
    skipped = [
        *jobs_with("Sonar quality gate", "skipped"),
        {"name": "Start the release of a proven merge", "conclusion": "skipped"},
    ]

    assert decide(early(tested_jobs=skipped)) == proven()


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (early(evidence=[evidence_artifact(base=OTHER)]), "main moved after CI run 6001"),
        (early(commit={"parents": [OTHER]}), "main moved after CI run 6001"),
        (
            early(commit={"parent_committed_at": "2026-11-21T04:45:00Z"}),
            "main moved after CI run 6001",
        ),
        (early(commit={"parent_committed_at": None}), "main moved after CI run 6001"),
        (early(tested={"head_sha": OTHER}), "tested its final head"),
        (early(pull={"head_sha": OTHER}), "tested its final head"),
        (early(tested={"event": "push"}), "tested its final head"),
        (early(tested={"workflow_id": 42}), "tested its final head"),
        (early(tested={"path": ".github/workflows/image-rescan.yml"}), "tested its final head"),
        (early(tested={"repository": FORK}), "tested its final head"),
        (early(tested={"head_repository": FORK}), "tested its final head"),
        (
            early(proof={"pull_requests": [], "run": None, "jobs": [], "artifacts": []}),
            "exactly one",
        ),
        (early(pull={"head_repository": FORK}), "a branch of this repository"),
        (early(pull={"head_repository": None}), "a branch of this repository"),
        (early(pull={"base_repository": FORK}), "a branch of this repository"),
        (early(pull={"base_ref": "release"}), "a branch of this repository"),
        (early(tested={"conclusion": "failure"}), "did not pass every job"),
        (early(tested={"conclusion": "cancelled"}), "did not pass every job"),
        (early(tested={"status": "in_progress", "conclusion": None}), "did not pass every job"),
        (early(tested_jobs=jobs_with("Windows tests (1/4)", "failure")), "did not pass every job"),
        (early(tested_jobs=jobs_with("Dependency audit", "cancelled")), "did not pass every job"),
        (
            early(tested_jobs=jobs_with("Verify (ubuntu-latest)", "skipped")),
            "did not pass every job",
        ),
        (early(tested_jobs=jobs_with("CI result", "skipped")), "did not pass every job"),
        (
            early(
                tested_jobs=[
                    {"name": name, "conclusion": "success"}
                    for name in PULL_REQUEST_JOBS
                    if name != "CI result"
                ]
            ),
            "did not pass every job",
        ),
        (early(evidence=[]), "no single tested tree"),
        (early(evidence=[evidence_artifact(attempt=2)]), "no single tested tree"),
        (
            early(evidence=[{**evidence_artifact(), "name": f"pr-tested-{PARENT}-{TREE}"}]),
            "no single",
        ),
        (early(evidence=[{**evidence_artifact(), "name": "pr-tested-main-tree-1"}]), "no single"),
        (
            early(evidence=[evidence_artifact(), evidence_artifact(tree=OTHER)]),
            "no single tested tree",
        ),
        (early(evidence=[evidence_artifact(), evidence_artifact()]), "no single tested tree"),
        (early(evidence=[evidence_artifact(expired=True)]), "expired or belongs to another head"),
        (early(evidence=[evidence_artifact(head_sha=OTHER)]), "expired or belongs to another head"),
        (early(evidence=[evidence_artifact(tree=OTHER)]), f"tested tree {OTHER}"),
        (early(commit={"tree": OTHER}), f"has tree {OTHER}"),
    ],
)
def test_an_early_release_needs_the_pull_request_run_of_this_exact_tree(
    value: dict[str, Any], reason: str
) -> None:
    deferred(value, reason)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        "deploy/release_authorization.py",
        "infra/terraform/main.tf",
    ],
)
def test_a_commit_that_changes_the_pipeline_waits_for_the_whole_run(path: str) -> None:
    deferred(early(commit={"paths": ["src/pyracast/views.py", path]}), f"pipeline ({path})")


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (early(commit={"parents": [PARENT, HEAD], "paths": []}), "not a squash merge"),
        (early(commit={"parents": [], "paths": []}), "not a squash merge"),
        (early(pull={"merge_commit_sha": OTHER}), "exactly one pull request"),
        (
            early(
                proof={
                    "pull_requests": [
                        {**early()["proof"]["pull_requests"][0], "number": number}
                        for number in (82, 83)
                    ],
                    "run": None,
                    "jobs": [],
                    "artifacts": [],
                }
            ),
            "exactly one pull request",
        ),
    ],
)
def test_a_merge_that_is_not_the_squash_of_one_pull_request_waits_for_the_whole_run(
    value: dict[str, Any], reason: str
) -> None:
    deferred(value, reason)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (early(run={"event": "workflow_dispatch"}), "push to main"),
        (early(run={"head_branch": "feature"}), "push to main"),
        (early(run={"head_repository": FORK}), "push to main"),
        (early(run={"workflow_id": 42}), "push to main"),
    ],
)
def test_a_ci_run_that_is_not_the_push_of_main_waits_for_the_whole_run(
    value: dict[str, Any], reason: str
) -> None:
    deferred(value, reason)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (early(proof={"error": "Request timed out"}), "Request timed out"),
        (early(proof=None), "unavailable"),
        (early(proof={"pull_requests": "unreadable"}), "malformed"),
        (early(proof={"pull_requests": [{"merge_commit_sha": MAIN}]}), "malformed"),
        (early(tested={"run_attempt": "first"}), "malformed"),
        (early(jobs=[{"conclusion": "success"}]), "malformed"),
    ],
)
def test_an_api_failure_or_malformed_evidence_waits_for_the_whole_run(
    value: dict[str, Any], reason: str
) -> None:
    deferred(value, reason)


def test_a_failed_git_read_waits_for_the_whole_run() -> None:
    value = early()
    value["commit"] = None

    deferred(value, "Git could not read")


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (early(run={"status": "completed", "conclusion": "success"}), "has ended"),
        (early(run={"status": "completed", "conclusion": "failure"}), "has ended"),
        (early(jobs=[{"name": IMAGE_JOB, "conclusion": None}]), "image job"),
        (early(jobs=[{"name": IMAGE_JOB, "conclusion": "failure"}]), "image job"),
        (early(jobs=[]), "image job"),
        (early(artifacts=[]), "image artifact"),
        (early(artifacts=[images(1, expired=True)]), "image artifact"),
        (early(artifacts=[images(1, head_sha=OLDER)]), "image artifact"),
        (early(run={"run_attempt": 2}, artifacts=[images(1, 90)]), "image artifact"),
    ],
)
def test_an_early_release_needs_the_images_this_attempt_built_while_the_run_goes_on(
    value: dict[str, Any], reason: str
) -> None:
    deferred(value, reason)


def test_an_early_release_of_a_commit_main_has_moved_past_is_superseded() -> None:
    decision = decide(early(event_sha=OLDER))

    assert isinstance(decision, Superseded)


def run_command(
    tmp_path: Path, *arguments: str, root: Path | None = None
) -> tuple[int, dict[str, str], str]:
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    status = release_authorization.main(
        list(arguments),
        {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)},
        root,
    )
    lines = output.read_text(encoding="utf-8").splitlines() if output.exists() else []
    written = dict(line.split("=", 1) for line in lines)
    return status, written, summary.read_text(encoding="utf-8") if summary.exists() else ""


def run_main(
    tmp_path: Path, value: dict[str, Any], root: Path | None = None
) -> tuple[int, dict[str, str], str]:
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return run_command(tmp_path, "authorize", str(path), root=root)


def test_main_writes_the_authorized_release_for_the_next_jobs(tmp_path: Path) -> None:
    status, outputs, summary = run_main(tmp_path, facts())

    assert status == 0
    assert outputs == {
        "authorized": "true",
        "sha": MAIN,
        "ci_run_id": "7001",
        "ci_run_attempt": "2",
        "artifact_id": "91",
        "ci_started_at": STARTED,
        "proven": "false",
    }
    assert summary == f"Authorized main commit {MAIN} using CI run 7001.\n"


def test_main_ends_a_superseded_run_as_a_skip(tmp_path: Path) -> None:
    status, outputs, summary = run_main(tmp_path, facts(event_sha=OLDER))

    assert status == 0
    assert outputs == {"authorized": "false"}
    assert summary.startswith("Superseded: ")


def test_main_fails_a_refused_run_and_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, outputs, summary = run_main(tmp_path, facts({"conclusion": "failure"}))

    assert status == 1
    assert outputs == {"authorized": "false"}
    assert summary.startswith("Refused: Only the current main SHA")
    assert "Refused: Only the current main SHA" in capsys.readouterr().err


def git(root: Path, *arguments: str, date: str = BEFORE_RUN) -> str:
    assert GIT is not None
    identity = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")
    # GIT_DIR and similar variables exported to hooks would redirect these commands into the
    # repository that runs the tests.
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment["GIT_AUTHOR_DATE"] = environment["GIT_COMMITTER_DATE"] = date
    result = subprocess.run(  # noqa: S603 -- Resolved git executable on a test repository.
        [GIT, "-C", str(root), *identity, "-c", "commit.gpgsign=false", *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


def commit(root: Path, *paths: str, date: str = BEFORE_RUN) -> str:
    for path in paths:
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("a", encoding="utf-8") as stream:
            stream.write("change\n")
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "--message", "change", date=date)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "--quiet", "--initial-branch=main")
    return root


def test_the_commit_facts_name_the_tree_the_single_parent_and_every_changed_path(
    repository: Path,
) -> None:
    parent = commit(repository, "deploy/release.sh", "src/a.py")
    git(repository, "mv", "deploy/release.sh", "scripts/release.sh")
    head = commit(repository, "src/a.py")

    assert commit_facts(repository, head) == {
        "tree": git(repository, "rev-parse", f"{head}^{{tree}}"),
        "parents": [parent],
        "paths": ["deploy/release.sh", "scripts/release.sh", "src/a.py"],
        "parent_committed_at": "2026-11-21T04:00:00+00:00",
    }


def test_the_commit_facts_of_a_merge_or_a_root_commit_list_no_paths(repository: Path) -> None:
    root = commit(repository, "a.txt")
    git(repository, "switch", "--quiet", "-c", "feature")
    side = commit(repository, "b.txt")
    git(repository, "switch", "--quiet", "main")
    commit(repository, "c.txt")
    git(repository, "merge", "--quiet", "--no-edit", "feature")
    merge = git(repository, "rev-parse", "HEAD")

    merged = commit_facts(repository, merge)
    first = commit_facts(repository, root)

    assert merged is not None
    assert first is not None
    assert (merged["parents"][1], merged["paths"]) == (side, [])
    assert (first["parents"], first["paths"], first["parent_committed_at"]) == ([], [], None)


def test_the_commit_facts_are_absent_when_git_cannot_read_the_commit(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = commit(repository, "a.txt")

    assert commit_facts(repository, OTHER) is None
    assert commit_facts(repository, "HEAD") is None
    monkeypatch.setattr(release_authorization.shutil, "which", lambda _: None)
    assert commit_facts(repository, head) is None


def evidence_for(repository: Path) -> dict[str, Any]:
    parent = commit(repository, "src/a.py")
    head = commit(repository, "src/b.py", date=STARTED)
    tree = git(repository, "rev-parse", f"{head}^{{tree}}")
    value = early(evidence=[evidence_artifact(base=parent, tree=tree)])
    value.pop("commit")
    for key in ("event_sha", "main_sha"):
        value[key] = head
    value["run"]["head_sha"] = head
    value["proof"]["pull_requests"][0]["merge_commit_sha"] = head
    value["artifacts"] = [
        {"id": 91, "name": f"release-images-{head}-1", "expired": False, "head_sha": head}
    ]
    return value


def test_main_releases_a_proven_merge_from_the_commit_git_reads(
    tmp_path: Path, repository: Path
) -> None:
    value = evidence_for(repository)

    status, outputs, summary = run_main(tmp_path, value, repository)

    assert status == 0
    assert outputs["authorized"] == "true"
    assert outputs["proven"] == "true"
    assert outputs["ci_run_attempt"] == "1"
    assert summary == (
        f"Authorized main commit {value['main_sha']} using CI run 7001 before its repeated "
        "jobs ended: CI run 6001 of pull request #82 passed on the same tree.\n"
    )


def test_main_defers_an_early_start_it_cannot_prove_without_failing(
    tmp_path: Path, repository: Path
) -> None:
    value = evidence_for(repository)
    value["proof"]["artifacts"] = []

    status, outputs, summary = run_main(tmp_path, value, repository)

    assert (status, outputs) == (0, {"authorized": "false"})
    assert summary.startswith("Deferred: CI run 6001 recorded no single tested tree")
    assert summary.endswith("the release after the whole CI run 7001 decides.\n")


def test_a_release_whose_commit_is_still_main_goes_on_to_deploy(tmp_path: Path) -> None:
    assert run_command(tmp_path, "current", MAIN, MAIN) == (0, {"current": "true"}, "")


def test_a_release_overtaken_by_a_newer_main_commit_ends_as_a_skip(tmp_path: Path) -> None:
    status, outputs, summary = run_command(tmp_path, "current", MAIN, OLDER)

    assert (status, outputs) == (0, {"current": "false"})
    assert summary == (
        f"Superseded: main moved from {OLDER} to {MAIN}; the release for {MAIN} deploys it.\n"
    )


@pytest.mark.parametrize("value", ["", "HEAD", "A" * 40])
def test_the_currency_check_refuses_an_unreadable_commit(tmp_path: Path, value: str) -> None:
    status, outputs, summary = run_command(tmp_path, "current", value, MAIN)

    assert (status, outputs) == (1, {"current": "false"})
    assert summary.startswith("Refused: ")


def current_after(
    tmp_path: Path, records: object, since: str = STARTED
) -> tuple[int, dict[str, str], str]:
    deployments = tmp_path / "deployments.json"
    deployments.write_text(json.dumps(records), encoding="utf-8")
    arguments = ("--deployments", str(deployments), "--since", since)
    return run_command(tmp_path, "current", MAIN, MAIN, *arguments)


def test_the_serialized_deploy_skips_a_commit_another_release_deployed_since_its_ci_run(
    tmp_path: Path,
) -> None:
    status, outputs, summary = current_after(tmp_path, [deployment()])

    assert (status, outputs) == (0, {"current": "false"})
    assert summary == (
        f"Already released: {MAIN} was deployed after its CI run started; nothing is transferred.\n"
    )


@pytest.mark.parametrize(
    ("records", "since"),
    [
        ([], STARTED),
        ([deployment(state="in_progress")], STARTED),
        ([deployment(state="failure")], STARTED),
        ([deployment(created_at=BEFORE_RUN)], STARTED),
        ([deployment(sha=OLDER)], STARTED),
        ([deployment()], ""),
        ({"unexpected": "shape"}, STARTED),
    ],
)
def test_the_serialized_deploy_goes_on_without_a_successful_release_since_the_ci_run(
    tmp_path: Path, records: object, since: str
) -> None:
    assert current_after(tmp_path, records, since) == (0, {"current": "true"}, "")


def test_the_serialized_deploy_goes_on_when_the_deployment_lookup_wrote_nothing(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "absent.json"
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{", encoding="utf-8")

    for path in (missing, corrupt):
        status = run_command(
            tmp_path, "current", MAIN, MAIN, "--deployments", str(path), "--since", STARTED
        )
        assert status[:2] == (0, {"current": "true"})
        (tmp_path / "output").unlink()


def test_outside_a_workflow_the_decision_needs_no_step_output_files() -> None:
    assert release_authorization.main(["current", MAIN, OLDER], {}) == 0
    assert release_authorization.main(["current", "HEAD", MAIN], {}) == 1
