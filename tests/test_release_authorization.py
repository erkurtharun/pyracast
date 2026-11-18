import json
from pathlib import Path
from typing import Any

import pytest
from deploy.release_authorization import Authorized, Refused, Superseded, decide

from deploy import release_authorization

MAIN = "a" * 40
OLDER = "b" * 40
REPOSITORY = "owner/pyracast"
WORKFLOW = 41


def facts(run: dict[str, Any] | None = None, **changes: Any) -> dict[str, Any]:
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
            **(run or {}),
        },
        "jobs": [
            {"name": "Verify (ubuntu-latest)", "conclusion": "success"},
            {"name": "CI result", "conclusion": "success"},
        ],
        "artifacts": [
            {"id": 91, "name": f"release-images-{MAIN}-2", "expired": False, "head_sha": MAIN},
            {"id": 92, "name": "coverage-2", "expired": False, "head_sha": MAIN},
        ],
    }
    value.update(changes)
    return value


@pytest.mark.parametrize("event", ["workflow_run", "workflow_dispatch"])
@pytest.mark.parametrize("run_event", ["push", "workflow_dispatch"])
def test_the_current_main_commit_with_a_passing_ci_result_is_authorized(
    event: str, run_event: str
) -> None:
    assert decide(facts({"event": run_event}, event=event)) == Authorized(
        sha=MAIN, ci_run_id=7001, ci_run_attempt=2, artifact_id=91
    )


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
    with pytest.raises(Refused, match="successful repository CI"):
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
    with pytest.raises(Refused, match="CI result"):
        decide(facts(jobs=jobs))


@pytest.mark.parametrize(
    "artifacts",
    [
        [],
        [{"id": 91, "name": f"release-images-{MAIN}-2", "expired": True, "head_sha": MAIN}],
        [{"id": 91, "name": f"release-images-{MAIN}-1", "expired": False, "head_sha": MAIN}],
        [{"id": 91, "name": f"release-images-{MAIN}-2", "expired": False, "head_sha": OLDER}],
        [
            {"id": 91, "name": f"release-images-{MAIN}-2", "expired": False, "head_sha": MAIN},
            {"id": 93, "name": f"release-images-{MAIN}-2", "expired": False, "head_sha": MAIN},
        ],
    ],
)
def test_exactly_one_unexpired_image_artifact_of_the_run_attempt_is_required(
    artifacts: list[dict[str, Any]],
) -> None:
    with pytest.raises(Refused, match="unexpired image artifact"):
        decide(facts(artifacts=artifacts))


@pytest.mark.parametrize("main", ["", "A" * 40, "a" * 39, None])
def test_an_unreadable_main_commit_is_refused(main: str | None) -> None:
    with pytest.raises(Refused, match="main commit"):
        decide(facts({"head_sha": main}, main_sha=main, event_sha=main))


def run_command(tmp_path: Path, *arguments: str) -> tuple[int, dict[str, str], str]:
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    status = release_authorization.main(
        list(arguments), {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}
    )
    lines = output.read_text(encoding="utf-8").splitlines() if output.exists() else []
    written = dict(line.split("=", 1) for line in lines)
    return status, written, summary.read_text(encoding="utf-8")


def run_main(tmp_path: Path, value: dict[str, Any]) -> tuple[int, dict[str, str], str]:
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return run_command(tmp_path, "authorize", str(path))


def test_main_writes_the_authorized_release_for_the_next_jobs(tmp_path: Path) -> None:
    status, outputs, summary = run_main(tmp_path, facts())

    assert status == 0
    assert outputs == {
        "authorized": "true",
        "sha": MAIN,
        "ci_run_id": "7001",
        "ci_run_attempt": "2",
        "artifact_id": "91",
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
