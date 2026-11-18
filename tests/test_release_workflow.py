import re
from pathlib import Path

from deploy import release_authorization, release_unit
from tests.support.workflows import (
    WORKFLOWS,
    ci_job_names,
    ci_jobs,
    commands,
    release_jobs,
    step,
    step_index,
    workflow,
)

ROOT = Path(__file__).resolve().parents[1]
TRANSFER = "Transfer the verified release and deploy through IAP"


def release_text() -> str:
    return (WORKFLOWS / "release.yml").read_text(encoding="utf-8")


def test_one_ci_result_job_judges_every_other_ci_job() -> None:
    jobs = ci_jobs()
    result = jobs["ci_result"]
    (check,) = [candidate for candidate in result["steps"] if "run" in candidate]

    assert result["name"] == release_authorization.REQUIRED_CI_JOB
    assert set(result["needs"]) == set(jobs) - {"ci_result"}
    assert result["if"] == "${{ !cancelled() }}"
    assert check["run"] == "python3 scripts/ci_result.py"
    assert check["env"] == {"NEEDS": "${{ toJSON(needs) }}"}


def test_the_release_waits_for_the_ci_result_job_alone() -> None:
    authorize = release_jobs()["authorize"]

    assert release_authorization.REQUIRED_CI_JOB in ci_job_names(ci_jobs())
    assert "const required" not in release_text()
    assert step(authorize, "Authorize the current main commit against its exact CI run")["run"] == (
        'python3 deploy/release_authorization.py authorize "$RUNNER_TEMP/release-facts.json"'
    )
    assert step_index(authorize, "actions/checkout") < step_index(
        authorize, "release_authorization.py authorize"
    )


def test_only_the_deploy_job_is_serialized_so_superseded_runs_skip_at_once() -> None:
    document = workflow("release.yml")
    jobs = release_jobs()

    assert "concurrency" not in document
    assert jobs["deploy"]["concurrency"] == {
        "group": "production-release",
        "cancel-in-progress": False,
    }
    assert [name for name, job in jobs.items() if "concurrency" in job] == ["deploy"]


def test_each_job_runs_only_after_the_decision_before_it() -> None:
    jobs = release_jobs()

    assert jobs["scan"]["needs"] == "authorize"
    assert jobs["scan"]["if"] == (
        "needs.authorize.outputs.authorized == 'true' && needs.authorize.outputs.release != 'false'"
    )
    assert jobs["deploy"]["needs"] == ["authorize", "scan"]
    assert jobs["deploy"]["if"] == "needs.scan.outputs.current == 'true'"
    assert jobs["cleanup"]["needs"] == ["authorize", "deploy"]
    for name in (
        "Find the commit of the newest production deployment when it succeeded",
        "Classify the changes since the deployed commit",
    ):
        assert step(jobs["authorize"], name)["if"] == "steps.decision.outputs.authorized == 'true'"


def test_every_image_is_scanned_before_any_cloud_credential_exists() -> None:
    jobs = release_jobs()
    scan = jobs["scan"]
    current = step(scan, "Skip a release that main has moved past")

    assert "environment" not in scan
    assert scan["permissions"] == {"contents": "read", "actions": "read"}
    assert (
        step_index(scan, "deploy/ci_images.py verify")
        < step_index(scan, "deploy/oci-images.sh load")
        < step_index(scan, "deploy/scan-images.sh")
        < scan["steps"].index(current)
    )
    assert (
        "bash deploy/scan-images.sh pyracast-backend:ci pyracast-web:ci pyracast-gateway:ci "
        '"$POSTGRES_IMAGE"'
    ) in commands(scan)
    assert current["run"] == (
        'python3 deploy/release_authorization.py current "$MAIN_SHA" "$RELEASE_SHA"'
    )
    assert scan["outputs"] == {"current": "${{ steps.current.outputs.current }}"}
    assert "aquasecurity/trivy-action" not in release_text()
    assert "deploy/smoke.sh" not in release_text()
    assert "scan-images.sh" not in commands(jobs["deploy"])


def test_ci_scans_all_five_images_before_merge() -> None:
    deployment = ci_jobs()["deployment"]
    scan = step(deployment, "Scan every image; block every high or critical vulnerability")

    assert (
        "bash deploy/scan-images.sh pyracast-backend:ci pyracast-web:ci pyracast-gateway:ci "
        '"$POSTGRES_IMAGE"'
    ) in scan["run"]
    assert deployment["steps"].index(scan) < step_index(deployment, "deploy/smoke.sh")
    assert not (ROOT / "deploy" / "scan-support.sh").exists()


def test_the_installer_runs_in_a_transient_unit_that_the_job_only_follows() -> None:
    deploy = release_jobs()["deploy"]
    transfer = step(deploy, TRANSFER)["run"]
    stage = '"/run/pyracast-release-$RELEASE_SHA-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"'

    assert "install-release.sh" not in release_text()
    assert f"stage={stage}" in transfer
    assert "release_unit.py' start '$stage' '$RELEASE_SHA' '$BACKEND_IMAGE'" in transfer
    assert "release_unit.py' follow '$stage'" in transfer
    assert re.search(r"while \(\( status == 255 && attempt < \d+ \)\)", transfer)
    assert release_unit.CONNECTION_LOST == 255
    assert "if sudo systemctl is-active --quiet '$unit'; then" in transfer
    assert 'unit="${stage#/run/}.service"' in transfer
    assert transfer.index("trap cleanup EXIT") < transfer.index("git archive")


def test_the_migration_secret_version_is_pinned_when_configured_and_numeric() -> None:
    deploy = release_jobs()["deploy"]
    validate = step(deploy, "Validate deployment configuration")["run"]
    transfer = step(deploy, TRANSFER)["run"]
    outputs = (ROOT / "infra" / "terraform" / "outputs.tf").read_text(encoding="utf-8")

    variable = "GCP_MIGRATION_SECRET_VERSION"
    assert deploy["env"][variable] == f"${{{{ vars.{variable} }}}}"
    assert (
        "[[ -z $GCP_MIGRATION_SECRET_VERSION || $GCP_MIGRATION_SECRET_VERSION =~ ^[1-9][0-9]*$ ]]"
        in validate
    )
    assert 'gcloud secrets versions access "${GCP_MIGRATION_SECRET_VERSION:-latest}"' in transfer
    assert "versions access latest" not in transfer
    assert re.search(
        r"GCP_MIGRATION_SECRET_VERSION\s*=\s*tostring\(var\.migration_secret_version\)", outputs
    )


def test_the_public_origin_is_probed_after_the_install_once_configured() -> None:
    deploy = release_jobs()["deploy"]
    probe = step(deploy, "Probe the public origin through the edge")
    outputs = (ROOT / "infra" / "terraform" / "outputs.tf").read_text(encoding="utf-8")

    assert probe["if"] == "vars.PYRACAST_PUBLIC_ORIGIN != ''"
    assert probe["env"] == {"PYRACAST_PUBLIC_ORIGIN": "${{ vars.PYRACAST_PUBLIC_ORIGIN }}"}
    assert probe["run"] == 'python3 deploy/probe_origin.py "$PYRACAST_PUBLIC_ORIGIN"'
    assert deploy["steps"].index(probe) > deploy["steps"].index(step(deploy, TRANSFER))
    assert re.search(r'PYRACAST_PUBLIC_ORIGIN\s*=\s*"https://\$\{var\.domain\}"', outputs)


def test_deploy_checks_out_the_authorized_commit_rather_than_a_moving_branch() -> None:
    jobs = release_jobs()
    for name in ("scan", "deploy"):
        checkout = jobs[name]["steps"][step_index(jobs[name], "actions/checkout")]
        assert checkout["with"]["ref"] == "${{ needs.authorize.outputs.sha }}", name
    assert "refs/heads/main" not in str(jobs["deploy"])


def test_only_the_first_windows_shard_repeats_the_static_stages() -> None:
    shards = ci_jobs()["verify_windows_shards"]
    partition = step(shards, "Run a complete Windows test partition")["run"]

    assert "${{ env.TEST_SHARD != '1' && '--tests-only' || '' }}" in partition


def test_the_web_dependency_audit_runs_once_per_ci_run() -> None:
    web = ci_jobs()["web_ubuntu"]

    assert step(web, "Audit web dependencies against dated exceptions")["if"] == (
        "runner.os == 'Linux'"
    )
