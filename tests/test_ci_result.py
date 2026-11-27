import json

import pytest

from scripts import ci_result

PASSED: dict[str, object] = {"result": "success", "outputs": {}}
EVENTS = ["pull_request", "push", "workflow_dispatch"]


def needs(planned: str = "true", **results: str) -> dict[str, dict[str, object]]:
    value: dict[str, dict[str, object]] = {
        name: dict(PASSED) for name in ("deployment", "verify_ubuntu", "audit", "sonar")
    }
    value["plan"] = {"result": "success", "outputs": {"audit": planned}}
    for name, result in results.items():
        value[name] = {"result": result, "outputs": {}}
    return value


@pytest.mark.parametrize("event", EVENTS)
def test_every_passing_job_passes_the_aggregate(event: str) -> None:
    assert ci_result.failures(needs(), event) == []


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_a_job_that_did_not_pass_fails_the_aggregate(event: str, result: str) -> None:
    assert ci_result.failures(needs(verify_ubuntu=result, deployment="success"), event) == [
        "verify_ubuntu"
    ]


@pytest.mark.parametrize("event", EVENTS)
def test_sonar_may_be_skipped_because_its_needs_are_judged_on_their_own(event: str) -> None:
    assert ci_result.failures(needs(sonar="skipped"), event) == []
    assert ci_result.failures(needs(sonar="skipped", verify_ubuntu="failure"), event) == [
        "verify_ubuntu"
    ]


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_a_sonar_run_that_did_not_pass_fails_the_aggregate(result: str) -> None:
    assert ci_result.failures(needs(sonar=result), "pull_request") == ["sonar"]


def test_a_pull_request_that_changes_no_audit_input_may_skip_the_audit() -> None:
    assert ci_result.failures(needs("false"), "pull_request") == []
    assert ci_result.failures(needs("false", audit="skipped"), "pull_request") == []


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_a_run_of_main_never_skips_the_audit_whatever_the_plan_says(event: str) -> None:
    assert ci_result.failures(needs("false", audit="skipped"), event) == ["audit"]


@pytest.mark.parametrize("outputs", [{"audit": "true"}, {"audit": "no"}, {}, None])
def test_a_pull_request_run_skips_the_audit_only_when_the_plan_said_false(
    outputs: dict[str, str] | None,
) -> None:
    value = needs(audit="skipped")
    value["plan"]["outputs"] = outputs

    assert ci_result.failures(value, "pull_request") == ["audit"]


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled"])
def test_a_plan_that_did_not_pass_fails_the_aggregate_and_excuses_no_audit(result: str) -> None:
    value = needs("false", audit="skipped")
    value["plan"]["result"] = result

    assert ci_result.failures(value, "pull_request") == ["audit", "plan"]


def test_a_failed_audit_fails_the_aggregate_whatever_the_plan_says() -> None:
    assert ci_result.failures(needs("false", audit="failure"), "pull_request") == ["audit"]


def test_every_failing_job_is_named_in_order() -> None:
    assert ci_result.failures(needs(verify_ubuntu="failure", deployment="cancelled"), "push") == [
        "deployment",
        "verify_ubuntu",
    ]


def test_main_reports_the_aggregate(capsys: pytest.CaptureFixture[str]) -> None:
    environ = {"GITHUB_EVENT_NAME": "push"}

    assert ci_result.main({**environ, "NEEDS": json.dumps(needs())}) == 0
    assert capsys.readouterr().out == "All 5 CI jobs passed.\n"
    assert ci_result.main({**environ, "NEEDS": json.dumps(needs(deployment="failure"))}) == 1
    assert capsys.readouterr().out == "These CI jobs did not pass: deployment.\n"


def test_main_judges_the_skips_by_the_event_of_the_run() -> None:
    skipped = json.dumps(needs("false", audit="skipped"))

    assert ci_result.main({"GITHUB_EVENT_NAME": "pull_request", "NEEDS": skipped}) == 0
    assert ci_result.main({"GITHUB_EVENT_NAME": "push", "NEEDS": skipped}) == 1


@pytest.mark.parametrize("value", ["", "[]", "{}", '{"a": {}}', '{"a": []}'])
def test_main_refuses_needs_it_cannot_read(value: str) -> None:
    with pytest.raises(ValueError, match="needs"):
        ci_result.main({"GITHUB_EVENT_NAME": "push", "NEEDS": value})
