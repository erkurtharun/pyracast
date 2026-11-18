import json

import pytest

from scripts import ci_result

PASSED = {"result": "success", "outputs": {}}


def needs(**results: str) -> dict[str, dict[str, object]]:
    value = {name: dict(PASSED) for name in ("deployment", "verify_ubuntu", "sonar")}
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


@pytest.mark.parametrize("value", ["", "[]", "{}", '{"a": {}}', '{"a": []}'])
def test_main_refuses_needs_it_cannot_read(value: str) -> None:
    with pytest.raises(ValueError, match="needs"):
        ci_result.main({"NEEDS": value})
