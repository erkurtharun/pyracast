import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from deploy import release_authorization
from deploy.release_authorization import (
    PROOF_FILE,
    Authorized,
    Proof,
    RefusedError,
    Superseded,
    UnprovenError,
    commit_facts,
    decide,
    merge_proof,
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
DEPLOYMENTS = "release-deployments.json"
BEFORE_RUN = "2026-11-21T04:00:00Z"
AFTER_START = "2026-11-21T05:10:00Z"
SECOND_LATER = "2026-11-21T05:10:01Z"
RERUN_STARTED = "2026-11-21T05:20:00Z"
PULL_RUN_CREATED = "2026-11-21T04:30:00Z"
RECORD_STARTED = "2026-11-21T04:50:00Z"
RECORDED = "2026-11-21T04:50:30Z"
RECORD_ENDED = "2026-11-21T04:51:00Z"
GIT = shutil.which("git")
IMAGE_JOB = "Deployment configuration and containers"
FIRST_ATTEMPT_STARTED = "2026-11-21T04:10:00Z"
UNBOUND_IMAGES = "Deployment configuration and containers job of that attempt"
SUITES = ("Verify (ubuntu-latest)", "Web (ubuntu-latest)", "Scientific (ubuntu-latest)")
IMAGE_BUILDS = {
    1: ("2026-11-21T04:11:00Z", "2026-11-21T04:18:00Z", "2026-11-21T04:19:00Z"),
    2: ("2026-11-21T05:01:00Z", "2026-11-21T05:08:00Z", "2026-11-21T05:09:00Z"),
}


def image_job(attempt: int, conclusion: str | None = "success", **changes: Any) -> dict[str, Any]:
    started, _, completed = IMAGE_BUILDS[attempt]
    job = {"name": IMAGE_JOB, "conclusion": conclusion}
    return job | {"started_at": started, "completed_at": completed} | changes


def run_jobs(image_attempt: int = 2) -> list[dict[str, Any]]:
    return [
        {"name": SUITES[0], "conclusion": "success"},
        image_job(image_attempt),
        {"name": "CI result", "conclusion": "success"},
        *({"name": name, "conclusion": "success"} for name in SUITES[1:]),
    ]


def facts(run_changes: dict[str, Any] | None = None, **changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "event": "workflow_run",
        "event_sha": MAIN,
        "main_sha": MAIN,
        "repository": REPOSITORY,
        "workflow_id": WORKFLOW,
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
        "jobs": run_jobs(),
        "artifacts": [
            images(2, 91),
            {"id": 92, "name": "coverage-2", "expired": False, "head_sha": MAIN},
        ],
        "earlier_attempts": [{"attempt": 1, "run_started_at": FIRST_ATTEMPT_STARTED}],
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
    uploaded = IMAGE_BUILDS[attempt][1] if attempt in IMAGE_BUILDS else None
    return {
        "id": artifact,
        "name": name,
        "expired": False,
        "head_sha": MAIN,
        "created_at": uploaded,
        **changes,
    }


def test_a_partial_rerun_releases_the_images_of_the_attempt_that_built_them() -> None:
    kept = facts(artifacts=[images(1, 90)], jobs=run_jobs(image_attempt=1))

    assert decide(kept) == authorized(ci_run_attempt=1, artifact_id=90)


def test_images_rebuilt_in_a_later_attempt_replace_the_earlier_ones() -> None:
    assert decide(facts(artifacts=[images(1, 91), images(2, 90)])) == authorized(artifact_id=90)


def test_a_later_ci_attempt_leaves_the_images_of_the_authorizing_attempt_in_place() -> None:
    artifacts = [images(1, 90), images(2, 91)]
    first = facts(
        {"run_attempt": 1, "run_started_at": FIRST_ATTEMPT_STARTED},
        artifacts=artifacts,
        jobs=run_jobs(image_attempt=1),
    )

    assert decide(first) == authorized(
        ci_run_attempt=1, artifact_id=90, ci_started_at=FIRST_ATTEMPT_STARTED
    )


@pytest.mark.parametrize(
    ("artifact", "jobs"),
    [
        (images(2, created_at=IMAGE_BUILDS[1][1]), run_jobs(image_attempt=1)),
        (images(2, created_at=IMAGE_BUILDS[1][1]), run_jobs()),
        (images(2, created_at="2026-11-21T05:30:00Z"), run_jobs(image_attempt=1)),
        (images(2, created_at="2026-11-21T05:00:59Z"), run_jobs()),
        (images(2, created_at="2026-11-21T05:09:01Z"), run_jobs()),
        (images(2, created_at=None), run_jobs()),
        (images(2), [job for job in run_jobs() if job["name"] != IMAGE_JOB]),
        (images(2), [*run_jobs(), image_job(2)]),
        (images(2), [*run_jobs()[:1], image_job(2, completed_at=None), *run_jobs()[2:]]),
    ],
    ids=[
        "named for a re-run but uploaded while the kept image job ran",
        "uploaded in an earlier attempt",
        "uploaded after the kept image job ended",
        "uploaded before the image job started",
        "uploaded after the image job ended",
        "no upload time",
        "no image job",
        "two image jobs",
        "image job without an end",
    ],
)
def test_only_images_uploaded_while_the_image_job_of_their_attempt_ran_are_released(
    artifact: dict[str, Any], jobs: list[dict[str, Any]]
) -> None:
    with pytest.raises(RefusedError, match=UNBOUND_IMAGES):
        decide(facts(artifacts=[images(1, 90), artifact], jobs=jobs))


def test_kept_images_need_the_start_of_the_attempt_their_name_carries() -> None:
    kept = facts(artifacts=[images(1, 90)], jobs=run_jobs(image_attempt=1), earlier_attempts=[])

    with pytest.raises(RefusedError, match=UNBOUND_IMAGES):
        decide(kept)


def test_images_uploaded_at_the_first_or_last_second_of_the_image_job_are_released() -> None:
    started, _, completed = IMAGE_BUILDS[2]
    for at in (started, completed):
        assert decide(facts(artifacts=[images(2, created_at=at)])) == authorized()


def test_images_named_for_an_earlier_attempt_but_created_in_the_rebuilt_image_job_are_refused() -> (
    None
):
    renamed = images(1, 90, created_at=IMAGE_BUILDS[2][1])

    with pytest.raises(RefusedError, match=UNBOUND_IMAGES):
        decide(facts(artifacts=[renamed], jobs=run_jobs()))


def without(value: dict[str, Any], key: str) -> dict[str, Any]:
    return {name: item for name, item in value.items() if name != key}


def absent_image_facts() -> list[dict[str, Any]]:
    kept_without_start = facts(
        artifacts=[images(1, 90)],
        jobs=run_jobs(image_attempt=1),
        earlier_attempts=[{"attempt": 1}],
    )
    run_without_start = facts()
    run_without_start["run"] = without(run_without_start["run"], "run_started_at")
    return [
        facts(artifacts=[without(images(2), "created_at")]),
        facts(jobs=[*run_jobs()[:1], without(image_job(2), "started_at"), *run_jobs()[2:]]),
        facts(jobs=[*run_jobs()[:1], without(image_job(2), "completed_at"), *run_jobs()[2:]]),
        kept_without_start,
        run_without_start,
    ]


ABSENT_IMAGE_FACTS = [
    "no creation time",
    "no image job start",
    "no image job end",
    "no start of the earlier attempt",
    "no start of the run's attempt",
]


@pytest.mark.parametrize("value", absent_image_facts(), ids=ABSENT_IMAGE_FACTS)
def test_a_whole_run_whose_image_binding_lacks_a_time_is_refused_and_says_why(
    tmp_path: Path, value: dict[str, Any]
) -> None:
    status, outputs, summary = run_main(tmp_path, value)

    assert (status, outputs) == (1, {"authorized": "false"})
    assert summary.startswith("Refused: The image artifact of attempt")


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


def deployment(state: str = "success", **changes: Any) -> dict[str, Any]:
    status = {"id": 301, "state": state, "created_at": AFTER_START}
    return {"sha": MAIN, "created_at": AFTER_START, "statuses": [status], **changes}


def test_the_release_after_the_whole_run_skips_a_commit_released_since_the_run_started() -> None:
    decision = decide(facts(deployments=[deployment()]))

    assert isinstance(decision, Superseded)
    assert f"{MAIN} is already released" in decision.reason


@pytest.mark.parametrize(
    "deployments",
    [
        None,
        [deployment("in_progress")],
        [deployment("queued")],
        [deployment("pending")],
        [deployment("failure")],
        [deployment("error")],
        [deployment("inactive")],
        [deployment(statuses=[])],
        [deployment(created_at=BEFORE_RUN)],
        [deployment(sha=OLDER)],
        [deployment(created_at="yesterday")],
        [deployment(statuses=[{"id": 301, "state": "success"}])],
        [{"sha": MAIN}],
        "unreadable",
    ],
)
def test_the_release_after_the_whole_run_deploys_without_a_successful_release_of_this_run(
    deployments: Any,
) -> None:
    assert decide(facts(deployments=deployments)) == authorized()


def statuses(*listed: tuple[int, str, str]) -> list[dict[str, Any]]:
    return [{"id": id_, "state": state, "created_at": at} for id_, state, at in listed]


@pytest.mark.parametrize(
    ("listed", "released"),
    [
        (statuses((10, "in_progress", AFTER_START), (11, "failure", AFTER_START)), False),
        (statuses((11, "failure", AFTER_START), (10, "in_progress", AFTER_START)), False),
        (statuses((10, "in_progress", AFTER_START), (11, "success", AFTER_START)), True),
        (statuses((11, "success", AFTER_START), (10, "in_progress", AFTER_START)), True),
        (statuses((12, "in_progress", AFTER_START), (5, "success", SECOND_LATER)), True),
        (statuses((5, "success", SECOND_LATER), (12, "failure", AFTER_START)), True),
        (statuses((5, "success", AFTER_START), (4, "failure", SECOND_LATER)), False),
    ],
)
def test_the_newest_deployment_status_is_chosen_by_time_then_by_id(
    listed: list[dict[str, Any]], released: bool
) -> None:
    record = deployment(statuses=listed)

    assert released_since([record], MAIN, STARTED, {"success"}) is released
    assert isinstance(decide(facts(deployments=[record])), Superseded) is released


def test_a_partial_rerun_of_main_after_a_release_of_its_earlier_images_is_superseded() -> None:
    for artifacts in ([], [images(1, 90)]):
        decision = decide(
            facts(
                {"run_started_at": RERUN_STARTED}, artifacts=artifacts, deployments=[deployment()]
            )
        )

        assert isinstance(decision, Superseded), artifacts
        assert f"{MAIN} is already released" in decision.reason


def test_a_full_rerun_of_main_that_rebuilt_its_images_is_judged_on_its_own() -> None:
    rebuilt = image_job(2, started_at="2026-11-21T05:21:00Z", completed_at="2026-11-21T05:29:00Z")
    value = facts(
        {"run_started_at": RERUN_STARTED},
        artifacts=[images(1, 90), images(2, 91, created_at="2026-11-21T05:28:00Z")],
        jobs=[*run_jobs()[:1], rebuilt, *run_jobs()[2:]],
        deployments=[deployment()],
    )

    assert decide(value) == authorized(ci_started_at=RERUN_STARTED)


def test_a_partial_rerun_measures_earlier_releases_from_the_creation_of_its_run() -> None:
    value = facts(
        {"run_started_at": RERUN_STARTED},
        artifacts=[images(1, 90)],
        jobs=run_jobs(image_attempt=1),
    )

    assert decide(value) == authorized(ci_run_attempt=1, artifact_id=90, ci_started_at=STARTED)


def test_an_operator_dispatch_releases_a_commit_already_released_from_its_run() -> None:
    dispatched = facts(event="workflow_dispatch", deployments=[deployment()])

    assert decide(dispatched) == authorized()


def test_the_released_check_needs_a_readable_start_of_the_ci_run() -> None:
    assert not released_since([deployment()], MAIN, None, {"success"})
    assert not released_since([deployment()], MAIN, "", {"success"})
    assert released_since([deployment()], MAIN, STARTED, {"success"})


SONAR = "Sonar quality gate"
RECORD = "Record the tested tree"
MERGE_PROOF = "Find the pull request run that passed on this tree"
STOP = "Stop the pull request run"
MERGED = "2026-11-21T04:52:00Z"
PULL_REQUEST_JOBS = [
    IMAGE_JOB,
    *SUITES,
    "Web production build (ubuntu-latest)",
    "Dependency audit",
    SONAR,
    "CI result",
    RECORD,
]
PROVEN = Proof(82, 6001)


def pull_request_job(name: str, conclusion: str | None, **changes: Any) -> dict[str, Any]:
    job: dict[str, Any] = {"name": name, "conclusion": conclusion}
    if name == RECORD:
        job |= {"started_at": RECORD_STARTED, "completed_at": RECORD_ENDED}
    return job | changes


def jobs_with(name: str, conclusion: str | None) -> list[dict[str, Any]]:
    return [
        pull_request_job(job, conclusion if job == name else "success") for job in PULL_REQUEST_JOBS
    ]


def evidence_artifact(
    base: str = PARENT, tree: str = TREE, attempt: int = 1, number: int = 82, **changes: Any
) -> dict[str, Any]:
    name = f"pr-tested-{number}-{base}-{tree}-{attempt}"
    return {
        "id": 52,
        "name": name,
        "expired": False,
        "head_sha": HEAD,
        "created_at": RECORDED,
        **changes,
    }


def proof_facts(
    *,
    pull: dict[str, Any] | None = None,
    tested: dict[str, Any] | None = None,
    tested_jobs: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    **changes: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "sha": MAIN,
        "repository": REPOSITORY,
        "workflow_id": WORKFLOW,
        "sonar_enabled": True,
        "pull_requests": [
            {
                "number": 82,
                "merge_commit_sha": MAIN,
                "merged_at": MERGED,
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
            else [pull_request_job(name, "success") for name in PULL_REQUEST_JOBS]
        ),
        "artifacts": (
            evidence
            if evidence is not None
            else [
                {"id": 51, "name": "pytest-results-1", "expired": False, "head_sha": HEAD},
                evidence_artifact(),
            ]
        ),
    }
    value.update(changes)
    return value


def commit_of_main(**changes: Any) -> dict[str, Any]:
    return {
        "tree": TREE,
        "parents": [PARENT],
        "paths": ["src/pyracast/views.py", "docs/DEPLOYMENT.md"],
        "parent_committed_at": BEFORE_RUN,
        **changes,
    }


def unproven(proof: object, reason: str, commit: object = None) -> None:
    with pytest.raises(UnprovenError) as refused:
        merge_proof(proof, commit_of_main() if commit is None else commit, MAIN)

    assert reason in str(refused.value), refused.value


def test_a_squash_merge_whose_pull_request_run_passed_on_the_same_tree_is_proven() -> None:
    assert merge_proof(proof_facts(), commit_of_main(), MAIN) == PROVEN


def test_the_pull_request_run_may_skip_the_first_job_of_a_push_and_sonar_while_sonar_is_off() -> (
    None
):
    push_only = [*jobs_with(SONAR, "success"), {"name": MERGE_PROOF, "conclusion": "skipped"}]
    sonar_off = [*jobs_with(SONAR, "skipped"), {"name": MERGE_PROOF, "conclusion": "skipped"}]

    assert merge_proof(proof_facts(tested_jobs=push_only), commit_of_main(), MAIN) == PROVEN
    off = proof_facts(tested_jobs=sonar_off, sonar_enabled=False)
    assert merge_proof(off, commit_of_main(), MAIN) == PROVEN


def test_a_pull_request_run_that_skipped_the_dependency_audit_is_no_proof() -> None:
    unproven(proof_facts(tested_jobs=jobs_with("Dependency audit", "skipped")), "every job")


@pytest.mark.parametrize(
    "name", [name for name in PULL_REQUEST_JOBS if name not in {"CI result", RECORD}]
)
@pytest.mark.parametrize("count", [0, 2])
def test_a_pull_request_run_must_record_each_required_job_once(name: str, count: int) -> None:
    jobs = [job for job in jobs_with(name, "success") if job["name"] != name]
    jobs.extend(pull_request_job(name, "success") for _ in range(count))

    unproven(proof_facts(tested_jobs=jobs), "every job")


def draft_run_jobs(result: str) -> list[dict[str, Any]]:
    return [
        pull_request_job(name, result if name == "CI result" else "skipped")
        for name in PULL_REQUEST_JOBS
    ]


@pytest.mark.parametrize(("result", "conclusion"), [("failure", "failure"), ("success", "success")])
def test_the_run_of_a_draft_pull_request_is_no_proof(result: str, conclusion: str) -> None:
    value = proof_facts(tested={"conclusion": conclusion}, tested_jobs=draft_run_jobs(result))

    unproven(value, "did not pass every job")


def test_a_pull_request_run_may_skip_its_stop_jobs() -> None:
    stops = [{"name": STOP, "conclusion": "skipped"} for _ in range(6)]
    tested = [*jobs_with(SONAR, "success"), *stops]

    assert merge_proof(proof_facts(tested_jobs=tested), commit_of_main(), MAIN) == PROVEN


@pytest.mark.parametrize("conclusion", ["failure", "cancelled"])
def test_a_pull_request_run_whose_stop_job_ended_without_success_is_no_proof(
    conclusion: str,
) -> None:
    stopped = [*jobs_with(SONAR, "success"), {"name": STOP, "conclusion": conclusion}]

    unproven(proof_facts(tested_jobs=stopped), "did not pass every job")


@pytest.mark.parametrize("enabled", [True, "false", None, "absent"])
def test_a_pull_request_run_that_skipped_sonar_while_it_is_on_is_no_proof(enabled: object) -> None:
    value = proof_facts(tested_jobs=jobs_with(SONAR, "skipped"), sonar_enabled=enabled)
    if enabled == "absent":
        value.pop("sonar_enabled")

    unproven(value, "did not pass every job")


def test_a_pull_request_run_whose_tree_record_failed_is_no_proof() -> None:
    unproven(proof_facts(tested_jobs=jobs_with(RECORD, "failure")), "did not pass every job")


def record_job(**changes: Any) -> list[dict[str, Any]]:
    others = [pull_request_job(name, "success") for name in PULL_REQUEST_JOBS if name != RECORD]
    return [*others, pull_request_job(RECORD, "success", **changes)]


@pytest.mark.parametrize(
    "value",
    [
        proof_facts(evidence=[evidence_artifact(created_at="2026-11-21T04:49:59Z")]),
        proof_facts(evidence=[evidence_artifact(created_at="2026-11-21T04:51:01Z")]),
        proof_facts(evidence=[evidence_artifact(created_at=None)]),
        proof_facts(tested_jobs=record_job(completed_at=None)),
        proof_facts(tested_jobs=record_job(started_at="soon")),
        proof_facts(
            tested_jobs=[*jobs_with(RECORD, "success"), pull_request_job(RECORD, "success")]
        ),
        proof_facts(
            tested_jobs=[job for job in jobs_with(RECORD, "success") if job["name"] != RECORD]
        ),
        proof_facts(
            tested={"run_attempt": 2, "run_started_at": "2026-11-21T04:55:00Z"},
            evidence=[evidence_artifact(attempt=2)],
        ),
        proof_facts(tested={"run_started_at": None}),
    ],
    ids=[
        "before the record job",
        "after the record job",
        "no record time",
        "record job without an end",
        "unreadable record job start",
        "two record jobs",
        "no record job",
        "record job kept from an earlier attempt",
        "no start of the tested attempt",
    ],
)
def test_only_a_record_made_while_the_record_job_ran_is_evidence(value: dict[str, Any]) -> None:
    unproven(value, "not recorded by its Record the tested tree job")


def test_a_record_made_at_the_first_or_last_second_of_the_record_job_is_evidence() -> None:
    for at in (RECORD_STARTED, RECORD_ENDED):
        value = proof_facts(evidence=[evidence_artifact(created_at=at)])
        assert merge_proof(value, commit_of_main(), MAIN) == PROVEN


@pytest.mark.parametrize(
    ("value", "commit", "reason"),
    [
        (proof_facts(evidence=[evidence_artifact(base=OTHER)]), {}, "main moved after CI run 6001"),
        (proof_facts(), {"parents": [OTHER]}, "main moved after CI run 6001"),
        (
            proof_facts(),
            {"parent_committed_at": "2026-11-21T04:45:00Z"},
            "main moved after CI run 6001",
        ),
        (proof_facts(), {"parent_committed_at": None}, "main moved after CI run 6001"),
        (proof_facts(tested={"head_sha": OTHER}), {}, "tested its final head"),
        (proof_facts(pull={"head_sha": OTHER}), {}, "tested its final head"),
        (proof_facts(tested={"event": "push"}), {}, "tested its final head"),
        (proof_facts(tested={"workflow_id": 42}), {}, "tested its final head"),
        (
            proof_facts(tested={"path": ".github/workflows/image-rescan.yml"}),
            {},
            "tested its final head",
        ),
        (proof_facts(tested={"repository": FORK}), {}, "tested its final head"),
        (proof_facts(tested={"head_repository": FORK}), {}, "tested its final head"),
        (proof_facts(run=None), {}, "tested its final head"),
        (proof_facts(pull_requests=[]), {}, "exactly one"),
        (proof_facts(pull={"head_repository": FORK}), {}, "a branch of this repository"),
        (proof_facts(pull={"head_repository": None}), {}, "a branch of this repository"),
        (proof_facts(pull={"base_repository": FORK}), {}, "a branch of this repository"),
        (proof_facts(pull={"base_ref": "release"}), {}, "a branch of this repository"),
        (proof_facts(tested={"conclusion": "failure"}), {}, "did not pass every job"),
        (proof_facts(tested={"conclusion": "cancelled"}), {}, "did not pass every job"),
        (
            proof_facts(tested={"status": "in_progress", "conclusion": None}),
            {},
            "did not pass every job",
        ),
        (
            proof_facts(tested_jobs=jobs_with("Web (ubuntu-latest)", "failure")),
            {},
            "did not pass every job",
        ),
        (
            proof_facts(tested_jobs=jobs_with("Web production build (ubuntu-latest)", "failure")),
            {},
            "did not pass every job",
        ),
        (
            proof_facts(tested_jobs=jobs_with("Dependency audit", "cancelled")),
            {},
            "did not pass every job",
        ),
        (
            proof_facts(tested_jobs=jobs_with("Verify (ubuntu-latest)", "skipped")),
            {},
            "did not pass every job",
        ),
        (proof_facts(tested_jobs=jobs_with("CI result", "skipped")), {}, "did not pass every job"),
        (
            proof_facts(
                tested_jobs=[
                    {"name": name, "conclusion": "success"}
                    for name in PULL_REQUEST_JOBS
                    if name != "CI result"
                ]
            ),
            {},
            "did not pass every job",
        ),
        (proof_facts(evidence=[]), {}, "no single tested tree"),
        (proof_facts(evidence=[evidence_artifact(attempt=2)]), {}, "no single tested tree"),
        (
            proof_facts(tested={"run_attempt": 2}, evidence=[evidence_artifact(attempt=1)]),
            {},
            "no single tested tree for attempt 2",
        ),
        (
            proof_facts(
                evidence=[{**evidence_artifact(), "name": f"pr-tested-82-{PARENT}-{TREE}"}]
            ),
            {},
            "no single",
        ),
        (
            proof_facts(evidence=[{**evidence_artifact(), "name": f"pr-tested-{PARENT}-{TREE}-1"}]),
            {},
            "no single",
        ),
        (
            proof_facts(evidence=[{**evidence_artifact(), "name": "pr-tested-82-main-tree-1"}]),
            {},
            "no single",
        ),
        (proof_facts(evidence=[evidence_artifact(number=83)]), {}, "names pull request #83"),
        (
            proof_facts(evidence=[evidence_artifact(), evidence_artifact(tree=OTHER)]),
            {},
            "no single tested tree",
        ),
        (
            proof_facts(evidence=[evidence_artifact(), evidence_artifact()]),
            {},
            "no single tested tree",
        ),
        (
            proof_facts(evidence=[evidence_artifact(expired=True)]),
            {},
            "expired or belongs to another head",
        ),
        (
            proof_facts(evidence=[evidence_artifact(head_sha=OTHER)]),
            {},
            "expired or belongs to another head",
        ),
        (proof_facts(evidence=[evidence_artifact(tree=OTHER)]), {}, f"tested tree {OTHER}"),
        (proof_facts(), {"tree": OTHER}, f"has tree {OTHER}"),
    ],
)
def test_a_proof_needs_the_pull_request_run_of_this_exact_tree(
    value: dict[str, Any], commit: dict[str, Any], reason: str
) -> None:
    unproven(value, reason, commit_of_main(**commit))


@pytest.mark.parametrize(
    ("merged", "proven"),
    [
        (PULL_RUN_CREATED, True),
        ("2026-11-22T04:30:00Z", True),
        ("2026-11-22T04:30:01Z", False),
        ("2026-11-21T04:29:59Z", False),
        (None, False),
        ("today", False),
    ],
    ids=[
        "merged as the run began",
        "merged a day after",
        "merged later than a day after",
        "merged before the run began",
        "no merge time",
        "unreadable merge time",
    ],
)
def test_only_a_pull_request_run_of_the_day_before_the_merge_is_a_proof(
    merged: object, proven: bool
) -> None:
    value = proof_facts(pull={"merged_at": merged})

    if proven:
        assert merge_proof(value, commit_of_main(), MAIN) == PROVEN
    else:
        unproven(value, "was not merged within 24 hours of CI run 6001")


PIPELINE = [
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    ".github/actions/setup/action.yml",
    ".github/dependabot.yml",
    "deploy/release_authorization.py",
    "deploy/compose.yaml",
    "infra/terraform/main.tf",
    "infra/terraform/.terraform.lock.hcl",
    "scripts/ci_result.py",
    "scripts/check.py",
    "scripts/check_scientific.py",
    "scripts/npm_audit.py",
    "scripts/start_postgres.py",
    "sonar-project.properties",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "Dockerfile",
    ".dockerignore",
    ".gitattributes",
    ".gitignore",
    ".editorconfig",
    "web/package.json",
    "web/package-lock.json",
    "web/Dockerfile",
    "web/.dockerignore",
    "web/.gitignore",
    "web/.prettierignore",
    "web/coverage-floor.json",
    "web/bundle-budget.json",
    "web/audit-exceptions.json",
    "web/eslint.config.mjs",
    "web/tsconfig.json",
    "web/playwright.config.ts",
    "web/playwright.integration.config.ts",
    "web/testing/run-coverage.mjs",
    "web/testing/coverage-floor.ts",
    "web/eslint/design-tokens.mjs",
    "web/tests/support/server.ts",
]


@pytest.mark.parametrize("path", PIPELINE)
def test_a_commit_that_changes_the_pipeline_is_never_proven(path: str) -> None:
    commit = commit_of_main(paths=["src/pyracast/views.py", path])

    unproven(proof_facts(), f"pipeline ({path})", commit)


@pytest.mark.parametrize(
    "path",
    [
        "src/pyracast/views.py",
        "tests/test_release_authorization.py",
        "tests/conftest.py",
        "web/src/app/page.tsx",
        "web/tests/workspace.spec.ts",
        "web/next.config.ts",
        "web/messages/en/core.json",
        "docs/DEPLOYMENT.md",
        "README.md",
        "deployment/notes.md",
        "scripts.md",
        "web/package.json.md",
        "src/pyproject.toml",
    ],
)
def test_product_tests_and_documents_are_outside_the_pipeline(path: str) -> None:
    assert merge_proof(proof_facts(), commit_of_main(paths=[path]), MAIN) == PROVEN


@pytest.mark.parametrize(
    ("value", "commit", "reason"),
    [
        (proof_facts(), {"parents": [PARENT, HEAD], "paths": []}, "not a squash merge"),
        (proof_facts(), {"parents": [], "paths": []}, "not a squash merge"),
        (proof_facts(pull={"merge_commit_sha": OTHER}), {}, "exactly one pull request"),
        (
            proof_facts(
                pull_requests=[
                    {**proof_facts()["pull_requests"][0], "number": number} for number in (82, 83)
                ]
            ),
            {},
            "exactly one pull request",
        ),
    ],
)
def test_a_merge_that_is_not_the_squash_of_one_pull_request_is_never_proven(
    value: dict[str, Any], commit: dict[str, Any], reason: str
) -> None:
    unproven(value, reason, commit_of_main(**commit))


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (proof_facts(sha=OTHER), f"name {OTHER}, not {MAIN}"),
        ({"error": "Request timed out"}, "unavailable: Request timed out"),
        (None, "unavailable"),
        ([], "unavailable"),
        (proof_facts(pull_requests="unreadable"), "malformed"),
        (proof_facts(pull_requests=[{"merge_commit_sha": MAIN}]), "malformed"),
        (proof_facts(tested={"run_attempt": "first"}), "malformed"),
        (proof_facts(jobs=[{"conclusion": "success"}]), "malformed"),
        (without(proof_facts(), "sha"), "malformed"),
    ],
)
def test_an_api_failure_or_malformed_facts_are_no_proof(value: object, reason: str) -> None:
    unproven(value, reason)


def test_a_failed_git_read_is_no_proof() -> None:
    with pytest.raises(UnprovenError, match="Git could not read the main commit"):
        merge_proof(proof_facts(), None, MAIN)


def skipped_suites(**changes: str | None) -> list[dict[str, Any]]:
    conclusions = dict.fromkeys(SUITES, "skipped") | changes
    return [
        image_job(2),
        {"name": "CI result", "conclusion": "success"},
        *({"name": name, "conclusion": conclusion} for name, conclusion in conclusions.items()),
    ]


def proven_run(proof: object = None, commit: object = None, **changes: Any) -> dict[str, Any]:
    value = facts(**({"jobs": skipped_suites()} | changes))
    value["proof"] = proof_facts() if proof is None else proof
    value["commit"] = commit_of_main() if commit is None else commit
    return value


def test_a_run_of_main_that_skipped_the_suites_a_pull_request_run_proved_is_released() -> None:
    assert decide(proven_run()) == authorized(proof=PROVEN)


def test_a_manually_dispatched_ci_run_must_run_every_suite() -> None:
    with pytest.raises(RefusedError, match="Only a push to main may skip"):
        decide(proven_run(run_changes={"event": "workflow_dispatch"}))


def test_a_run_of_main_that_ran_every_suite_needs_no_proof() -> None:
    value = facts(proof={"error": "Request timed out"}, commit=None)

    assert decide(value) == authorized()


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (proven_run(proof={"error": "Request timed out"}), "Request timed out"),
        (proven_run(proof=proof_facts(evidence=[])), "no single tested tree"),
        (proven_run(commit=commit_of_main(tree=OTHER)), f"has tree {OTHER}"),
        (proven_run(proof=proof_facts(sha=OLDER)), f"name {OLDER}"),
        (proven_run(proof=proof_facts(pull={"merged_at": "2026-11-23T00:00:00Z"})), "24 hours"),
    ],
)
def test_a_run_of_main_that_skipped_suites_without_a_proof_is_refused(
    value: dict[str, Any], reason: str
) -> None:
    with pytest.raises(
        RefusedError, match="CI run 7001 skipped jobs that no pull request"
    ) as refused:
        decide(value)

    assert reason in str(refused.value)


def test_one_skipped_suite_also_needs_the_proof() -> None:
    one = proven_run(jobs=skipped_suites(**{SUITES[0]: "success", SUITES[1]: "success"}))

    assert decide(one) == authorized(proof=PROVEN)
    one["proof"] = None
    with pytest.raises(RefusedError, match="no pull request run proved"):
        decide(one)


@pytest.mark.parametrize(
    "jobs",
    [
        skipped_suites(**{SUITES[0]: "failure"}),
        skipped_suites(**{SUITES[1]: "cancelled"}),
        skipped_suites(**{SUITES[2]: None}),
        [job for job in skipped_suites() if job["name"] != SUITES[0]],
        [*skipped_suites(), {"name": SUITES[0], "conclusion": "skipped"}],
        [*run_jobs(), {"name": SUITES[2], "conclusion": "success"}],
    ],
    ids=["failed", "cancelled", "running", "missing", "twice skipped", "twice passed"],
)
def test_a_run_of_main_must_end_each_suite_once_by_passing_or_skipping(
    jobs: list[dict[str, Any]],
) -> None:
    with pytest.raises(RefusedError, match="did not end each of"):
        decide(proven_run(jobs=jobs))


def run_command(
    tmp_path: Path, *arguments: str, root: Path | None = None, sha: str | None = None
) -> tuple[int, dict[str, str], str]:
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    environ = {
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
        "RUNNER_TEMP": str(tmp_path),
    }
    if sha is not None:
        environ["GITHUB_SHA"] = sha
    status = release_authorization.main(list(arguments), environ, root)
    lines = output.read_text(encoding="utf-8").splitlines() if output.exists() else []
    written = dict(line.split("=", 1) for line in lines)
    return status, written, summary.read_text(encoding="utf-8") if summary.exists() else ""


def write_proof(tmp_path: Path, proof: object) -> Path:
    path = tmp_path / PROOF_FILE
    path.write_text(json.dumps(proof), encoding="utf-8")
    return path


def run_main(
    tmp_path: Path, value: dict[str, Any], root: Path | None = None
) -> tuple[int, dict[str, str], str]:
    path = tmp_path / "release-facts.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    proof = write_proof(tmp_path, value.get("proof"))
    return run_command(tmp_path, "authorize", str(path), str(proof), root=root)


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
    (repository / "scripts").mkdir()
    git(repository, "mv", "deploy/release.sh", "scripts/release.sh")
    head = commit(repository, "src/a.py")

    assert commit_facts(repository, head) == {
        "tree": git(repository, "rev-parse", f"{head}^{{tree}}"),
        "parents": [parent],
        "paths": ["deploy/release.sh", "scripts/release.sh", "src/a.py"],
        "parent_committed_at": git(repository, "log", "-1", "--format=%cI", parent),
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


def test_the_commit_facts_keep_each_changed_path_exactly_as_git_names_it(
    repository: Path,
) -> None:
    parent = commit(repository, "src/a.py")
    blob = git(repository, "hash-object", "-w", "src/a.py")
    git(repository, "update-index", "--add", "--cacheinfo", "100644", blob, " deploy.md")
    git(repository, "commit", "--quiet", "--message", "change")
    head = git(repository, "rev-parse", "HEAD")

    facts_of_head = commit_facts(repository, head)

    assert facts_of_head is not None
    assert (facts_of_head["parents"], facts_of_head["paths"]) == ([parent], [" deploy.md"])


def test_a_one_commit_rebase_onto_an_unmoved_main_has_the_facts_of_a_squash(
    repository: Path,
) -> None:
    base = commit(repository, "src/a.py")
    git(repository, "switch", "--quiet", "-c", "feature")
    first = commit(repository, "src/b.py", date=STARTED)
    second = commit(repository, "src/c.py", date=STARTED)
    git(repository, "switch", "--quiet", "--detach", base)
    git(repository, "merge", "--quiet", "--squash", first)
    git(repository, "commit", "--quiet", "--message", "squash", date=AFTER_START)
    squash = git(repository, "rev-parse", "HEAD")
    git(repository, "switch", "--quiet", "--detach", base)
    git(repository, "cherry-pick", first, date=AFTER_START)
    rebased_first = git(repository, "rev-parse", "HEAD")
    git(repository, "cherry-pick", second, date=AFTER_START)
    rebased_last = git(repository, "rev-parse", "HEAD")

    squashed = commit_facts(repository, squash)
    rebased = commit_facts(repository, rebased_last)

    assert squash != rebased_first
    assert squashed is not None
    assert squashed["parents"] == [base]
    assert commit_facts(repository, rebased_first) == squashed
    assert rebased is not None
    assert rebased["parents"] == [rebased_first] != [base]


def test_the_commit_facts_are_absent_when_git_cannot_read_the_commit(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = commit(repository, "a.txt")

    assert commit_facts(repository, OTHER) is None
    assert commit_facts(repository, "HEAD") is None
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert commit_facts(repository, head) is None


def evidence_for(repository: Path) -> tuple[str, dict[str, Any]]:
    parent = commit(repository, "src/a.py")
    head = commit(repository, "src/b.py", date=STARTED)
    tree = git(repository, "rev-parse", f"{head}^{{tree}}")
    proof = proof_facts(sha=head, evidence=[evidence_artifact(base=parent, tree=tree)])
    proof["pull_requests"][0]["merge_commit_sha"] = head
    return head, proof


def released_from(repository: Path) -> dict[str, Any]:
    head, proof = evidence_for(repository)
    value = facts(jobs=skipped_suites(), event_sha=head, main_sha=head, proof=proof)
    value["run"]["head_sha"] = head
    value["artifacts"] = [{**images(2, 91), "name": f"release-images-{head}-2", "head_sha": head}]
    return value


def test_main_releases_a_run_whose_skipped_suites_the_commit_git_reads_proves(
    tmp_path: Path, repository: Path
) -> None:
    value = released_from(repository)

    status, outputs, summary = run_main(tmp_path, value, repository)

    assert (status, outputs["authorized"], outputs["ci_run_attempt"]) == (0, "true", "2")
    assert summary == (
        f"Authorized main commit {value['main_sha']} using CI run 7001; CI run 6001 of pull "
        "request #82 passed the jobs it skipped on the same tree.\n"
    )


def test_main_refuses_a_run_whose_skipped_suites_nothing_proves(
    tmp_path: Path, repository: Path
) -> None:
    value = released_from(repository)
    value["proof"]["artifacts"] = []

    status, outputs, summary = run_main(tmp_path, value, repository)

    assert (status, outputs) == (1, {"authorized": "false"})
    assert summary == (
        "Refused: CI run 7001 skipped jobs that no pull request run proved: CI run 6001 "
        "recorded no single tested tree for attempt 1\n"
    )


def test_the_first_job_of_a_push_proves_a_commit_git_reads(
    tmp_path: Path, repository: Path
) -> None:
    head, proof = evidence_for(repository)

    status, outputs, summary = run_command(
        tmp_path, "prove", str(write_proof(tmp_path, proof)), root=repository, sha=head
    )

    assert (status, outputs) == (0, {"proven": "true"})
    assert summary == (
        "CI run 6001 of pull request #82 passed every job on this tree; Scientific "
        "(ubuntu-latest), Verify (ubuntu-latest), Web (ubuntu-latest) are not run again.\n"
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            lambda proof, path: write_proof(path.parent, {**proof, "artifacts": []}),
            "CI run 6001 recorded no single tested",
        ),
        (
            lambda _, path: write_proof(path.parent, {"error": "Server Error"}),
            "unavailable: Server Error",
        ),
        (lambda _, path: path.unlink(), "unavailable: None"),
        (lambda _, path: path.write_text("{", encoding="utf-8"), "unavailable: None"),
    ],
    ids=["no tested tree", "lookup failed", "no facts", "unreadable facts"],
)
def test_the_first_job_of_a_push_runs_every_job_when_nothing_proves_the_commit(
    tmp_path: Path,
    repository: Path,
    change: Callable[[dict[str, Any], Path], object],
    reason: str,
) -> None:
    head, proof = evidence_for(repository)
    path = write_proof(tmp_path, proof)
    change(proof, path)

    status, outputs, summary = run_command(tmp_path, "prove", str(path), root=repository, sha=head)

    assert (status, outputs) == (0, {"proven": "false"})
    assert summary.startswith("Every job runs: ")
    assert reason in summary


@pytest.mark.parametrize("sha", [None, "", OTHER])
def test_the_first_job_of_a_push_needs_a_commit_git_can_read(
    tmp_path: Path, repository: Path, sha: str | None
) -> None:
    _, proof = evidence_for(repository)

    status, outputs, summary = run_command(
        tmp_path, "prove", str(write_proof(tmp_path, proof)), root=repository, sha=sha
    )

    assert (status, outputs) == (0, {"proven": "false"})
    assert summary.startswith("Every job runs: Git could not read the main commit")


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
    deployments = tmp_path / DEPLOYMENTS
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
    "records",
    [
        [],
        [deployment(state="in_progress")],
        [deployment(state="failure")],
        [deployment(created_at=BEFORE_RUN)],
        [deployment(sha=OLDER)],
    ],
)
def test_the_serialized_deploy_goes_on_without_a_successful_release_since_the_ci_run(
    tmp_path: Path, records: object
) -> None:
    assert current_after(tmp_path, records) == (
        0,
        {"current": "true", "deployments": "read"},
        "",
    )


@pytest.mark.parametrize(
    ("records", "since"),
    [
        ([deployment()], ""),
        ({"unexpected": "shape"}, STARTED),
        ([{"sha": MAIN}], STARTED),
        ([deployment(statuses=[{"state": "success"}])], STARTED),
    ],
)
def test_the_serialized_deploy_goes_on_and_says_the_deployments_are_unknown_when_unreadable(
    tmp_path: Path, records: object, since: str
) -> None:
    assert current_after(tmp_path, records, since) == (
        0,
        {"current": "true", "deployments": "unknown"},
        "",
    )


def test_the_serialized_deploy_goes_on_when_the_deployment_lookup_wrote_nothing(
    tmp_path: Path,
) -> None:
    path = tmp_path / DEPLOYMENTS

    for content in (None, "{"):
        if content is not None:
            path.write_text(content, encoding="utf-8")
        status = run_command(
            tmp_path, "current", MAIN, MAIN, "--deployments", str(path), "--since", STARTED
        )
        assert status[:2] == (0, {"current": "true", "deployments": "unknown"})
        (tmp_path / "output").unlink()


def current_with_lookup(
    tmp_path: Path, path: Path, lookup: str, main: str = MAIN
) -> tuple[int, dict[str, str], str]:
    arguments = ("--deployments", str(path), "--since", STARTED, "--deployment-lookup", lookup)
    return run_command(tmp_path, "current", main, MAIN, *arguments)


def test_a_decision_without_a_deployment_lookup_reports_no_deployment_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / DEPLOYMENTS

    for content in (None, json.dumps([deployment()])):
        if content is not None:
            path.write_text(content, encoding="utf-8")
        assert current_with_lookup(tmp_path, path, "false") == (0, {"current": "true"}, "")
        (tmp_path / "output").unlink()
    path.unlink()
    status, outputs, summary = current_with_lookup(tmp_path, path, "false", main=OLDER)
    assert (status, outputs) == (0, {"current": "false"})
    assert summary.startswith("Superseded: ")


def test_a_decision_after_an_asked_deployment_lookup_reports_what_it_read(
    tmp_path: Path,
) -> None:
    path = tmp_path / DEPLOYMENTS

    assert current_with_lookup(tmp_path, path, "true")[:2] == (
        0,
        {"current": "true", "deployments": "unknown"},
    )
    (tmp_path / "output").unlink()
    path.write_text("[]", encoding="utf-8")
    assert current_with_lookup(tmp_path, path, "true")[:2] == (
        0,
        {"current": "true", "deployments": "read"},
    )


@pytest.mark.parametrize("lookup", ["", "unknown", "True"])
def test_a_decision_refuses_an_unreadable_deployment_lookup_flag(
    tmp_path: Path, lookup: str
) -> None:
    with pytest.raises(SystemExit) as refused:
        current_with_lookup(tmp_path, tmp_path / DEPLOYMENTS, lookup)

    assert refused.value.code == 2


def test_outside_a_workflow_the_decision_needs_no_step_output_files() -> None:
    assert release_authorization.main(["current", MAIN, OLDER], {}) == 0
    assert release_authorization.main(["current", "HEAD", MAIN], {}) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        ["authorize", "elsewhere/release-facts.json", f"runner/{PROOF_FILE}"],
        ["authorize", "runner/other.json", f"runner/{PROOF_FILE}"],
        ["authorize", "runner/release-facts.json", f"elsewhere/{PROOF_FILE}"],
        ["authorize", "runner/release-facts.json", "runner/other.json"],
        ["prove", f"elsewhere/{PROOF_FILE}"],
        ["prove", "runner/release-facts.json"],
    ],
)
def test_the_authorization_and_the_proof_read_only_their_runner_temp_files(
    tmp_path: Path, arguments: list[str]
) -> None:
    for directory in ("runner", "elsewhere"):
        (tmp_path / directory).mkdir()
    files = [tmp_path / name for name in arguments[1:]]
    for file in files:
        file.write_text(json.dumps(facts()), encoding="utf-8")
    environ = {"RUNNER_TEMP": str(tmp_path / "runner"), "GITHUB_OUTPUT": str(tmp_path / "output")}

    with pytest.raises(SystemExit) as refused:
        release_authorization.main([arguments[0], *map(str, files)], environ)

    assert refused.value.code == 2
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    ("directory", "name"),
    [("elsewhere", DEPLOYMENTS), ("runner", "records.json")],
)
def test_the_release_decision_refuses_any_deployment_records_but_the_runner_temp_file(
    tmp_path: Path, directory: str, name: str
) -> None:
    (tmp_path / "runner").mkdir()
    requested = tmp_path / directory / name
    requested.parent.mkdir(exist_ok=True)
    requested.write_text(json.dumps([deployment()]), encoding="utf-8")
    environ = {"RUNNER_TEMP": str(tmp_path / "runner"), "GITHUB_OUTPUT": str(tmp_path / "output")}
    arguments = ["current", MAIN, MAIN, "--deployments", str(requested), "--since", STARTED]

    with pytest.raises(SystemExit) as refused:
        release_authorization.main(arguments, environ)

    assert refused.value.code == 2
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("runner_temp", [{}, {"RUNNER_TEMP": ""}])
@pytest.mark.parametrize(
    ("arguments", "name", "content"),
    [
        (["authorize", "release-facts.json", PROOF_FILE], "release-facts.json", facts()),
        (["prove", PROOF_FILE], PROOF_FILE, {}),
        (
            ["current", MAIN, MAIN, "--deployments", DEPLOYMENTS, "--since", STARTED],
            DEPLOYMENTS,
            [deployment()],
        ),
    ],
)
def test_a_command_reading_a_file_refuses_to_run_without_the_runner_temp_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    runner_temp: dict[str, str],
    arguments: list[str],
    name: str,
    content: object,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / name).write_text(json.dumps(content), encoding="utf-8")
    environ = {**runner_temp, "GITHUB_OUTPUT": str(tmp_path / "output")}

    with pytest.raises(SystemExit) as exit_info:
        release_authorization.main(arguments, environ)

    assert exit_info.value.code == 2
    assert "RUNNER_TEMP is required" in capsys.readouterr().err
    assert not (tmp_path / "output").exists()


def test_a_run_attempt_without_the_starts_of_its_earlier_attempts_is_refused_at_once() -> None:
    huge = facts(
        run_changes={"run_attempt": 10**12},
        artifacts=[images(1, 90)],
        jobs=run_jobs(image_attempt=1),
        earlier_attempts=[],
    )

    with pytest.raises(RefusedError, match=UNBOUND_IMAGES):
        decide(huge)
