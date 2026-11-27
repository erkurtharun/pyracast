import json

import pytest

from scripts import ci_result

PASSED: dict[str, object] = {"result": "success", "outputs": {}}


def needs(**results: str) -> dict[str, dict[str, object]]:
    value: dict[str, dict[str, object]] = {
        name: dict(PASSED) for name in ("deployment", "verify_ubuntu", "sonar")
    }
    for name, result in results.items():
        value[name] = {"result": result, "outputs": {}}
    return value


def test_every_passing_job_passes_the_aggregate() -> None:
    assert ci_result.failures(needs()) == []


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_a_job_that_did_not_pass_fails_the_aggregate(result: str) -> None:
    assert ci_result.failures(needs(verify_ubuntu=result, deployment="success")) == [
        "verify_ubuntu"
    ]


def test_sonar_may_be_skipped_because_its_needs_are_judged_on_their_own() -> None:
    assert ci_result.failures(needs(sonar="skipped")) == []
    assert ci_result.failures(needs(sonar="skipped", verify_ubuntu="failure")) == ["verify_ubuntu"]


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_a_sonar_run_that_did_not_pass_fails_the_aggregate(result: str) -> None:
    assert ci_result.failures(needs(sonar=result)) == ["sonar"]


def test_every_failing_job_is_named_in_order() -> None:
    assert ci_result.failures(needs(verify_ubuntu="failure", deployment="cancelled")) == [
        "deployment",
        "verify_ubuntu",
    ]


def test_main_reports_the_aggregate(capsys: pytest.CaptureFixture[str]) -> None:
    assert ci_result.main({"NEEDS": json.dumps(needs())}) == 0
    assert capsys.readouterr().out == "All 3 CI jobs passed.\n"
    assert ci_result.main({"NEEDS": json.dumps(needs(deployment="failure"))}) == 1
    assert capsys.readouterr().out == "These CI jobs did not pass: deployment.\n"


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
    assert ci_result.main({"PULL_REQUEST_DRAFT": draft, "NEEDS": json.dumps(needs())}) == 0
    assert ci_result.main({"PULL_REQUEST_DRAFT": draft, "NEEDS": json.dumps(draft_needs())}) == 1


@pytest.mark.parametrize("value", ["", "[]", "{}", '{"a": {}}', '{"a": []}'])
def test_main_refuses_needs_it_cannot_read(value: str) -> None:
    with pytest.raises(ValueError, match="needs"):
        ci_result.main({"NEEDS": value})
