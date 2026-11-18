import re
from pathlib import Path
from typing import Any

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


def test_the_cleanup_removes_every_image_build_of_the_authorizing_run_up_to_the_released_one() -> (
    None
):
    cleanup = release_jobs()["cleanup"]
    (remove,) = cleanup["steps"]
    script = " ".join(remove["with"]["script"].split())

    assert cleanup["permissions"] == {"actions": "write"}
    assert remove["env"] == {
        "RELEASE_SHA": "${{ needs.authorize.outputs.sha }}",
        "CI_RUN_ID": "${{ needs.authorize.outputs.ci_run_id }}",
        "CI_RUN_ATTEMPT": "${{ needs.authorize.outputs.ci_run_attempt }}",
    }
    assert "outputs.artifact_id" not in str(cleanup)
    assert "context.payload" not in script
    assert (
        "github.paginate(github.rest.actions.listWorkflowRunArtifacts, "
        "{ ...context.repo, run_id: Number(runId), per_page: 100, })"
    ) in script
    assert "new RegExp(`^release-images-${sha}-([1-9][0-9]*)$`)" in script
    assert "Number(match[1]) <= Number(released) && !artifact.expired" in script
    assert "first.attempt - second.attempt || first.id - second.id" in script
    assert script.endswith(
        "for (const { id } of builds) { await github.rest.actions.deleteArtifact"
        "({ ...context.repo, artifact_id: id }); }"
    )


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


SCAN = "Scan every image; block every high or critical vulnerability"


def triggers(name: str) -> dict[str, Any]:
    document = workflow(name)
    found: dict[str, Any] = document["on"] if "on" in document else document[True]
    return found


def image_builds(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        candidate["with"]
        for candidate in job["steps"]
        if str(candidate.get("uses", "")).startswith("docker/build-push-action@")
    ]


def test_the_release_images_are_rescanned_daily_without_cloud_credentials() -> None:
    document = workflow("image-rescan.yml")
    (job,) = document["jobs"].values()
    built = [(build["context"], build.get("file"), build["tags"]) for build in image_builds(job)]
    released = [
        (build["context"], build.get("file"), build["tags"])
        for build in image_builds(ci_jobs()["deployment"])
    ]

    assert re.fullmatch(r"\d+ \d+ \* \* \*", triggers("image-rescan.yml")["schedule"][0]["cron"])
    assert "workflow_dispatch" in triggers("image-rescan.yml")
    assert document["permissions"] == {"contents": "read"}
    assert "permissions" not in job
    assert "environment" not in job
    assert built == released
    assert all(build["load"] is True for build in image_builds(job))
    assert step(job, SCAN)["run"] == step(ci_jobs()["deployment"], SCAN)["run"].replace(
        'echo "POSTGRES_IMAGE=$POSTGRES_IMAGE" >> "$GITHUB_ENV"\n', ""
    )
    assert triggers("release.yml")["workflow_run"]["workflows"] == ["CI"]
    assert document["name"] != "CI"


def test_the_serialized_deploy_cancels_a_release_main_moved_past_just_before_the_transfer() -> None:
    deploy = release_jobs()["deploy"]
    steps = deploy["steps"]
    read = step(deploy, "Read the current main commit")
    current = step(deploy, "Decide whether main has moved past this release")
    cancel = step(deploy, "Cancel a release that main has moved past")
    transfer = step(deploy, TRANSFER)

    assert "getBranch" in read["with"]["script"]
    assert current["env"] == {"MAIN_SHA": "${{ steps.main.outputs.sha }}"}
    assert current["run"] == (
        'python3 deploy/release_authorization.py current "$MAIN_SHA" "$RELEASE_SHA"'
    )
    assert cancel["if"] == "steps.current.outputs.current != 'true'"
    assert "cancelWorkflowRun" in cancel["with"]["script"]
    assert "core.setFailed" in cancel["with"]["script"]
    assert [steps.index(read), steps.index(current), steps.index(cancel)] == [
        steps.index(transfer) - 3,
        steps.index(transfer) - 2,
        steps.index(transfer) - 1,
    ]
    assert "if" not in transfer
    assert deploy["permissions"]["actions"] == "write"


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
    assert transfer.endswith('trap - EXIT\ncleanup\nexit "$status"\n')


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


def test_every_release_probes_the_public_origin_or_warns_that_none_is_set() -> None:
    deploy = release_jobs()["deploy"]
    probe = step(deploy, "Probe the public origin through the edge")
    outputs = (ROOT / "infra" / "terraform" / "outputs.tf").read_text(encoding="utf-8")

    assert "if" not in probe
    assert probe["env"] == {"PYRACAST_PUBLIC_ORIGIN": "${{ vars.PYRACAST_PUBLIC_ORIGIN }}"}
    assert probe["run"] == 'python3 deploy/probe_origin.py "$PYRACAST_PUBLIC_ORIGIN"'
    assert deploy["steps"].index(probe) > deploy["steps"].index(step(deploy, TRANSFER))
    assert re.search(r'PYRACAST_PUBLIC_ORIGIN\s*=\s*"https://\$\{var\.domain\}"', outputs)


def test_scan_checks_out_the_authorized_commit() -> None:
    scan = release_jobs()["scan"]
    checkout = scan["steps"][step_index(scan, "actions/checkout")]

    assert checkout["with"]["ref"] == "${{ needs.authorize.outputs.sha }}"


def test_deploy_runs_the_authorized_commit_only_after_proving_it_is_on_main() -> None:
    deploy = release_jobs()["deploy"]
    checkout, move = deploy["steps"][:2]

    assert step_index(deploy, "actions/checkout") == 0
    assert step_index(deploy, "git checkout") == 1
    assert checkout["with"] == {
        "repository": "erkurtharun/pyracast",
        "ref": "refs/heads/main",
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    assert move["name"] == "Move to the authorized commit, which must be on main"
    assert move["run"].splitlines() == [
        '[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]',
        'git merge-base --is-ancestor "$RELEASE_SHA" HEAD',
        'git checkout --detach "$RELEASE_SHA"',
    ]
    assert deploy["env"]["RELEASE_SHA"] == "${{ needs.authorize.outputs.sha }}"
    assert "refs/heads/main" not in str(deploy["steps"][1:])


def test_only_the_first_windows_shard_repeats_the_static_stages() -> None:
    shards = ci_jobs()["verify_windows_shards"]
    partition = step(shards, "Run a complete Windows test partition")["run"]

    assert "${{ env.TEST_SHARD != '1' && '--tests-only' || '' }}" in partition


def test_the_web_dependency_audit_runs_once_per_ci_run() -> None:
    web = ci_jobs()["web_ubuntu"]

    assert step(web, "Audit web dependencies against dated exceptions")["if"] == (
        "runner.os == 'Linux'"
    )
