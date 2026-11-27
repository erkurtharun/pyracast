import json
from typing import Any

import pytest

from scripts import ci_result

PASSED: dict[str, object] = {"result": "success", "outputs": {}}
SUITES = ("verify_ubuntu", "web_ubuntu", "scientific")


def needs(**results: str) -> dict[str, dict[str, object]]:
    value: dict[str, dict[str, object]] = {
        name: dict(PASSED) for name in ("deployment", "verify_ubuntu", "sonar")
    }
    for name, result in results.items():
        value[name] = {"result": result, "outputs": {}}
    return value


def push_needs(proof: str = "success", proven: object = "true", **results: str) -> dict[str, Any]:
    value: dict[str, Any] = {
        "merge_proof": {"result": proof, "outputs": {"proven": proven}},
        "deployment": dict(PASSED),
        "web_production": dict(PASSED),
        "audit": dict(PASSED),
        **{name: {"result": "skipped", "outputs": {}} for name in (*SUITES, "sonar")},
    }
    for name, result in results.items():
        value[name] = {"result": result, "outputs": {}}
    return value


def test_every_passing_job_passes_the_aggregate() -> None:
    assert ci_result.failures(needs(), "pull_request") == []


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_a_job_that_did_not_pass_fails_the_aggregate(result: str) -> None:
    assert ci_result.failures(needs(verify_ubuntu=result, deployment="success"), "push") == [
        "verify_ubuntu"
    ]


def test_sonar_may_be_skipped_because_its_needs_are_judged_on_their_own() -> None:
    assert ci_result.failures(needs(sonar="skipped"), "push") == []
    assert ci_result.failures(needs(sonar="skipped", verify_ubuntu="failure"), "push") == [
        "verify_ubuntu"
    ]


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_a_sonar_run_that_did_not_pass_fails_the_aggregate(result: str) -> None:
    assert ci_result.failures(needs(sonar=result), "push") == ["sonar"]


def test_every_failing_job_is_named_in_order() -> None:
    assert ci_result.failures(
        needs(verify_ubuntu="failure", deployment="cancelled"), "pull_request"
    ) == ["deployment", "verify_ubuntu"]


def test_a_push_proven_by_its_first_job_may_skip_the_suites_it_proved() -> None:
    assert ci_result.failures(push_needs(), "push") == []


@pytest.mark.parametrize(
    ("value", "event"),
    [
        (push_needs(proven="false"), "push"),
        (push_needs(proven=""), "push"),
        (push_needs(proven=True), "push"),
        (push_needs(proof="failure"), "push"),
        (push_needs(proof="skipped"), "push"),
        (push_needs(), "pull_request"),
        (push_needs(), "workflow_dispatch"),
        (push_needs(), ""),
    ],
    ids=[
        "not proven",
        "no answer",
        "an answer that is no string",
        "failed first job",
        "skipped first job",
        "pull request",
        "dispatch",
        "unknown event",
    ],
)
def test_skipped_suites_fail_without_a_proof_of_this_push(
    value: dict[str, Any], event: str
) -> None:
    assert set(SUITES) <= set(ci_result.failures(value, event))


def test_a_proven_push_still_needs_every_job_bound_to_its_commit() -> None:
    for name in ("deployment", "web_production", "audit"):
        assert ci_result.failures(push_needs(**{name: "skipped"}), "push") == [name]


def test_a_push_whose_first_job_did_not_pass_fails_even_when_every_suite_ran() -> None:
    ran = {name: "success" for name in SUITES}

    assert ci_result.failures(push_needs(proof="failure", **ran), "push") == ["merge_proof"]
    assert ci_result.failures(push_needs(proof="skipped", **ran), "push") == ["merge_proof"]
    assert ci_result.failures(push_needs(proven="false", **ran), "push") == []


@pytest.mark.parametrize("event", ["pull_request", "workflow_dispatch"])
def test_a_run_that_is_not_a_push_skips_the_first_job(event: str) -> None:
    ran = {name: "success" for name in SUITES}

    assert ci_result.failures(push_needs(proof="skipped", **ran), event) == []


def test_main_reports_the_aggregate(capsys: pytest.CaptureFixture[str]) -> None:
    assert ci_result.main({"NEEDS": json.dumps(needs()), "GITHUB_EVENT_NAME": "push"}) == 0
    assert capsys.readouterr().out == "All 3 CI jobs passed.\n"
    assert ci_result.main({"NEEDS": json.dumps(needs(deployment="failure"))}) == 1
    assert capsys.readouterr().out == "These CI jobs did not pass: deployment.\n"


def test_main_names_the_suites_a_proven_push_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    assert ci_result.main({"NEEDS": json.dumps(push_needs()), "GITHUB_EVENT_NAME": "push"}) == 0
    assert capsys.readouterr().out == (
        "All 8 CI jobs passed or were passed on this tree by the pull request.\n"
    )
    assert ci_result.main({"NEEDS": json.dumps(push_needs()), "GITHUB_EVENT_NAME": ""}) == 1
    assert capsys.readouterr().out == (
        "These CI jobs did not pass: scientific, verify_ubuntu, web_ubuntu.\n"
    )


def draft_needs() -> dict[str, dict[str, object]]:
    return {name: {"result": "skipped"} for name in needs()}


@pytest.mark.parametrize("run", [draft_needs(), needs()], ids=["jobs skipped", "jobs passed"])
def test_a_draft_pull_request_run_is_never_a_pass(
    run: dict[str, dict[str, object]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert ci_result.main({"PULL_REQUEST_DRAFT": "true", "NEEDS": json.dumps(run)}) == 1
    assert capsys.readouterr().out == (
        "A draft pull request is not verified; mark it ready for review to run CI.\n"
    )


@pytest.mark.parametrize("draft", ["false", ""])
def test_a_pull_request_that_is_not_a_draft_is_judged_by_its_jobs(draft: str) -> None:
    environ = {"PULL_REQUEST_DRAFT": draft, "GITHUB_EVENT_NAME": "pull_request"}

    assert ci_result.main({**environ, "NEEDS": json.dumps(needs())}) == 0
    assert ci_result.main({**environ, "NEEDS": json.dumps(draft_needs())}) == 1


@pytest.mark.parametrize("value", ["", "[]", "{}", '{"a": {}}', '{"a": []}'])
def test_main_refuses_needs_it_cannot_read(value: str) -> None:
    with pytest.raises(ValueError, match="needs"):
        ci_result.main({"NEEDS": value})
