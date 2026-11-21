import re
from pathlib import Path
from typing import Any

from deploy import ci_images, release_authorization, release_unit
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


def test_one_ci_result_job_judges_every_other_ci_job_but_the_release_start_and_record() -> None:
    jobs = ci_jobs()
    result = jobs["ci_result"]
    checkout, check = result["steps"]

    assert result["name"] == release_authorization.REQUIRED_CI_JOB
    assert set(result["needs"]) == set(jobs) - {"ci_result", "release_start", "pr_tested"}
    assert result["if"] == "${{ !cancelled() }}"
    assert str(checkout["uses"]).startswith("actions/checkout@")
    assert check["name"] == "Require every CI job"
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


SCAN = "Scan every image; block every high or critical vulnerability"
RELEASE_IMAGES = ("backend", "web", "gateway", "postgres", "rabbitmq")
CONTEXTS = {
    "backend": (".", "Dockerfile"),
    "web": ("web", "web/Dockerfile"),
    "gateway": ("deploy/gateway", None),
    "postgres": ("deploy/postgres", None),
    "rabbitmq": ("deploy/rabbitmq", None),
}
# The support images run under their immutable identity, which the scan and the smoke share.
SCANNED = [
    "POSTGRES_IMAGE=$(docker image inspect pyracast-postgres:ci --format '{{.Id}}')",
    "RABBITMQ_IMAGE=$(docker image inspect pyracast-rabbitmq:ci --format '{{.Id}}')",
    '[[ "$POSTGRES_IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]]',
    '[[ "$RABBITMQ_IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]]',
    "bash deploy/scan-images.sh pyracast-backend:ci pyracast-web:ci pyracast-gateway:ci "
    '"$POSTGRES_IMAGE" "$RABBITMQ_IMAGE"',
]
BROKER_ROUND_TRIP = "Move queued work to the built broker and back to the previous public broker"
HANDED_TO_SMOKE = [
    'echo "POSTGRES_IMAGE=$POSTGRES_IMAGE" >> "$GITHUB_ENV"',
    'echo "RABBITMQ_IMAGE=$RABBITMQ_IMAGE" >> "$GITHUB_ENV"',
]


def scanned(job: dict[str, Any]) -> list[str]:
    return [line for line in step(job, SCAN)["run"].splitlines() if "$GITHUB_ENV" not in line]


AUTOMATIC = "github.event_name == 'workflow_run' || inputs.ci_run_id != ''"
FIND_DEPLOYMENTS = "Find the production deployments of this commit"
FIND_BEFORE_DOWNLOAD = "Find the production deployments of this commit before the download"
READ_BEFORE_DOWNLOAD = "Read the current main commit before the download"
DECIDE_BEFORE_DOWNLOAD = "Decide before the download whether this release is still needed"
CURRENT_WITH_RECORDS = (
    'python3 deploy/release_authorization.py current "$MAIN_SHA" "$RELEASE_SHA" '
    '--deployments "$RUNNER_TEMP/release-deployments.json" --since "$CI_STARTED_AT"'
)
CURRENT_ENVIRONMENT = {
    "MAIN_SHA": "${{ steps.main.outputs.sha }}",
    "CI_STARTED_AT": "${{ needs.authorize.outputs.ci_started_at }}",
}


def first_download(job: dict[str, Any]) -> int:
    return step_index(job, "actions/download-artifact@")


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
    for name in (
        "Verify source, run and immutable archive contents",
        "Load the verified images",
        SCAN,
    ):
        assert step(scan, name)["if"] == "steps.download.outcome == 'success'", name
    assert step(scan, SCAN)["run"].splitlines() == SCANNED
    assert current["env"] == CURRENT_ENVIRONMENT
    assert current["run"] == CURRENT_WITH_RECORDS
    assert scan["outputs"] == {"current": "${{ steps.current.outputs.current }}"}
    assert "aquasecurity/trivy-action" not in release_text()
    assert "deploy/smoke.sh" not in release_text()
    assert "scan-images.sh" not in commands(jobs["deploy"])


def test_a_later_release_ends_before_its_downloads_once_the_commit_is_released() -> None:
    jobs = release_jobs()
    for identifier, stop in (
        ("scan", None),
        ("deploy", "Cancel a release that is no longer needed before the download"),
    ):
        job = jobs[identifier]
        steps = job["steps"]
        find = step(job, FIND_BEFORE_DOWNLOAD)
        read = step(job, READ_BEFORE_DOWNLOAD)
        decision = step(job, DECIDE_BEFORE_DOWNLOAD)
        download = steps[first_download(job)]

        assert find["if"] == AUTOMATIC, identifier
        assert find["continue-on-error"] is True, identifier
        assert "getBranch" in read["with"]["script"], identifier
        assert decision["id"] == "before", identifier
        assert decision["env"] == {
            "MAIN_SHA": "${{ steps.main-before.outputs.sha }}",
            "CI_STARTED_AT": "${{ needs.authorize.outputs.ci_started_at }}",
        }, identifier
        assert decision["run"] == CURRENT_WITH_RECORDS, identifier
        assert steps.index(find) < steps.index(read) < steps.index(decision) < steps.index(download)
        assert step_index(job, "actions/checkout@") < steps.index(find), identifier
        if stop is None:
            assert download["if"] == "steps.before.outputs.current == 'true'"
        else:
            cancel = step(job, stop)
            assert cancel["if"] == "steps.before.outputs.current != 'true'"
            assert "cancelWorkflowRun" in cancel["with"]["script"]
            assert steps.index(decision) + 1 == steps.index(cancel) < steps.index(download)
            assert "if" not in download


def test_a_later_scan_ends_without_failing_when_a_finished_release_removed_its_images() -> None:
    scan = release_jobs()["scan"]
    steps = scan["steps"]
    download = steps[first_download(scan)]
    refuse = step(scan, "Refuse images that vanished without a release of this commit")

    assert download["id"] == "download"
    assert download["continue-on-error"] is True
    assert (
        step(scan, FIND_DEPLOYMENTS)["if"]
        == f"steps.before.outputs.current == 'true' && ({AUTOMATIC})"
    )
    assert (
        step(scan, "Read the current main commit")["if"] == "steps.before.outputs.current == 'true'"
    )
    assert step(scan, "Skip a release that main has moved past")["if"] == (
        "steps.before.outputs.current == 'true'"
    )
    assert refuse["if"] == (
        "steps.download.outcome == 'failure' && steps.current.outputs.current == 'true'"
    )
    assert refuse["run"].splitlines()[-1] == "exit 1"
    assert steps.index(refuse) == len(steps) - 1


def test_ci_scans_all_five_images_before_merge() -> None:
    deployment = ci_jobs()["deployment"]
    scan = step(deployment, SCAN)
    environment = [line for line in scan["run"].splitlines() if "$GITHUB_ENV" in line]

    assert scanned(deployment) == SCANNED
    assert environment == HANDED_TO_SMOKE
    assert deployment["steps"].index(scan) < step_index(deployment, "deploy/smoke.sh")
    assert not (ROOT / "deploy" / "scan-support.sh").exists()


def test_the_compose_smoke_runs_the_five_images_ci_built_and_scanned() -> None:
    deployment = ci_jobs()["deployment"]
    smoke = step(deployment, "Exercise the production images and Compose boundary")

    assert smoke["run"] == (
        "bash deploy/smoke.sh pyracast-backend:ci pyracast-web:ci pyracast-gateway:ci "
        '"$POSTGRES_IMAGE" "$RABBITMQ_IMAGE"'
    )


def test_ci_moves_queued_work_to_the_built_broker_and_back_before_it_keeps_the_images() -> None:
    deployment = ci_jobs()["deployment"]
    round_trip = step(deployment, BROKER_ROUND_TRIP)

    assert round_trip["run"] == (
        'bash deploy/test-broker-rollback.sh pyracast-backend:ci "$RABBITMQ_IMAGE"'
    )
    assert (
        step_index(deployment, "deploy/smoke.sh")
        < deployment["steps"].index(round_trip)
        < step_index(deployment, "actions/upload-artifact")
    )


def test_ci_builds_each_release_image_once_into_the_archive_the_release_reads() -> None:
    deployment = ci_jobs()["deployment"]
    builds = {
        candidate["id"]: candidate["with"]
        for candidate in deployment["steps"]
        if str(candidate.get("uses", "")).startswith("docker/build-push-action@")
    }
    bind = step(deployment, "Bind the image archives to this exact CI run")

    assert tuple(builds) == RELEASE_IMAGES == ci_images.IMAGES
    for name, build in builds.items():
        context, dockerfile = CONTEXTS[name]
        assert build["context"] == context
        assert build.get("file") == dockerfile
        assert build["platforms"] == "linux/amd64"
        assert build["tags"] == f"pyracast-{name}:ci"
        assert build["outputs"] == (
            f"type=oci,dest=${{{{ runner.temp }}}}/release-images/{name}.oci.tar"
        )
        assert build["provenance"] == "mode=max"
        assert build["sbom"] == builds["backend"]["sbom"]
        assert build["labels"] == "org.opencontainers.image.revision=${{ github.sha }}"
        assert build["cache-from"] == f"type=gha,scope={name}-linux-amd64,timeout=2m"
    assert bind["env"] == {
        f"{name.upper()}_DIGEST": f"${{{{ steps.{name}.outputs.digest }}}}"
        for name in RELEASE_IMAGES
    }
    assert f"for image in {' '.join(RELEASE_IMAGES)}; do" in bind["run"]


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


DEPLOYED_SCAN = "Scan the deployed images; block every high or critical vulnerability"
WITHOUT_READER = "env.IMAGE_RESCAN == ''"
WITH_READER = "env.IMAGE_RESCAN != ''"


def test_without_its_reader_setting_the_image_rescan_rebuilds_and_scans_main_daily() -> None:
    document = workflow("image-rescan.yml")
    (job,) = document["jobs"].values()
    built = [(build["context"], build.get("file"), build["tags"]) for build in image_builds(job)]
    released = [
        (build["context"], build.get("file"), build["tags"])
        for build in image_builds(ci_jobs()["deployment"])
    ]
    builds = [
        candidate
        for candidate in job["steps"]
        if str(candidate.get("uses", "")).startswith("docker/build-push-action@")
    ]

    assert re.fullmatch(r"\d+ \d+ \* \* \*", triggers("image-rescan.yml")["schedule"][0]["cron"])
    assert "workflow_dispatch" in triggers("image-rescan.yml")
    assert document["permissions"] == {"contents": "read"}
    assert job["name"] == "Rescan the release images"
    assert job["env"]["IMAGE_RESCAN"] == "${{ vars.GCP_IMAGE_RESCAN }}"
    assert "environment" not in job
    assert built == released
    assert all(build["with"]["load"] is True for build in builds)
    assert {candidate["if"] for candidate in builds} == {WITHOUT_READER}
    assert step(job, SCAN)["if"] == WITHOUT_READER
    assert step(job, SCAN)["run"].splitlines() == SCANNED
    assert triggers("release.yml")["workflow_run"]["workflows"] == ["CI"]
    assert document["name"] != "CI"


def test_with_its_reader_setting_the_image_rescan_scans_the_deployed_digests() -> None:
    (job,) = workflow("image-rescan.yml")["jobs"].values()
    reader = step(job, "Read the registry reader of the deployed images")
    (authenticate,) = [
        candidate
        for candidate in job["steps"]
        if str(candidate.get("uses", "")).startswith("google-github-actions/auth@")
    ]
    deployed = step(job, DEPLOYED_SCAN)
    images = " ".join(f'"$REPOSITORY/{name}:production"' for name in RELEASE_IMAGES)

    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert reader["if"] == authenticate["if"] == deployed["if"] == WITH_READER
    assert reader["run"] == 'python3 deploy/image_rescan.py "$IMAGE_RESCAN" >> "$GITHUB_OUTPUT"'
    assert authenticate["with"] == {
        "workload_identity_provider": "${{ steps.reader.outputs.provider }}",
        "service_account": "${{ steps.reader.outputs.service_account }}",
        "token_format": "access_token",
    }
    assert deployed["env"] == {
        "REGISTRY_ACCESS_TOKEN": "${{ steps.google-auth.outputs.access_token }}",
        "REPOSITORY": "${{ steps.reader.outputs.repository }}",
    }
    assert deployed["run"].splitlines() == [
        "printf '%s' \"$REGISTRY_ACCESS_TOKEN\" | docker login -u oauth2accesstoken "
        '--password-stdin "https://${REPOSITORY%%/*}"',
        f"for image in {' '.join(RELEASE_IMAGES)}; do",
        '  docker pull --quiet --platform linux/amd64 "$REPOSITORY/$image:production"',
        "done",
        f"bash deploy/scan-images.sh {images}",
    ]
    assert (
        job["steps"].index(reader) < job["steps"].index(authenticate) < job["steps"].index(deployed)
    )


def test_the_serialized_deploy_cancels_a_release_main_moved_past_just_before_the_transfer() -> None:
    deploy = release_jobs()["deploy"]
    steps = deploy["steps"]
    read = step(deploy, "Read the current main commit")
    current = step(deploy, "Decide whether main has moved past this release")
    cancel = step(deploy, "Cancel a release that main has moved past")
    transfer = step(deploy, TRANSFER)

    assert "getBranch" in read["with"]["script"]
    assert current["env"] == {
        "MAIN_SHA": "${{ steps.main.outputs.sha }}",
        "CI_STARTED_AT": "${{ needs.authorize.outputs.ci_started_at }}",
    }
    assert current["run"] == (
        'python3 deploy/release_authorization.py current "$MAIN_SHA" "$RELEASE_SHA" '
        '--deployments "$RUNNER_TEMP/release-deployments.json" --since "$CI_STARTED_AT"'
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
    assert (
        "release_unit.py' start '$stage' '$RELEASE_SHA' '$BACKEND_IMAGE' '$WEB_IMAGE' "
        "'$GATEWAY_IMAGE' '$POSTGRES_IMAGE' '$RABBITMQ_IMAGE'\""
    ) in transfer
    assert "release_unit.py' follow '$stage'" in transfer
    assert re.search(r"while \(\( status == 255 && attempt < \d+ \)\)", transfer)
    assert release_unit.CONNECTION_LOST == 255
    assert "if sudo systemctl is-active --quiet '$unit'; then" in transfer
    assert 'unit="${stage#/run/}.service"' in transfer
    assert transfer.index("trap cleanup EXIT") < transfer.index("git archive")
    assert transfer.endswith('trap - EXIT\ncleanup\nexit "$status"\n')


def test_the_release_records_the_five_digests_it_deployed() -> None:
    record = step(release_jobs()["deploy"], "Record the deployed digests")

    assert record["run"].splitlines() == [
        r"printf 'Released %s\n\nBackend: %s\n\nWeb: %s\n\nGateway: %s\n\nPostgreSQL: %s"
        r"\n\nRabbitMQ: %s\n' "
        '"$RELEASE_SHA" "$BACKEND_IMAGE" "$WEB_IMAGE" "$GATEWAY_IMAGE" "$POSTGRES_IMAGE" '
        '"$RABBITMQ_IMAGE" >> "$GITHUB_STEP_SUMMARY"'
    ]


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


RELEASE_TESTED_JOBS = {
    "deployment",
    "verify_ubuntu",
    "verify_windows_shards",
    "verify_windows",
    "web_ubuntu",
    "web_windows",
    "scientific",
    "audit",
}
PINNED_ACTION = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}")
PARENT = "c" * 40
TREE = "d" * 40


def test_ci_asks_release_to_judge_a_push_to_main_once_its_images_are_ready() -> None:
    start = ci_jobs()["release_start"]
    (ask,) = start["steps"]
    script = " ".join(ask["with"]["script"].split())

    assert start["name"] == release_authorization.RELEASE_START_JOB
    assert start["needs"] == "deployment"
    assert start["if"] == (
        "vars.GCP_DEPLOY_ENABLED == 'true' && github.event_name == 'push' && "
        "github.ref == 'refs/heads/main'"
    )
    assert start["permissions"] == {"actions": "write"}
    assert start["continue-on-error"] is True
    assert str(ask["uses"]).startswith("actions/github-script@")
    assert script == (
        "await github.rest.actions.createWorkflowDispatch({ ...context.repo, "
        "workflow_id: 'release.yml', ref: 'main', inputs: { ci_run_id: String(context.runId), "
        "ci_run_attempt: process.env.GITHUB_RUN_ATTEMPT }, });"
    )


def test_every_repeated_job_still_runs_to_the_end_on_each_push_to_main() -> None:
    document = workflow("ci.yml")
    jobs = ci_jobs()

    assert triggers("ci.yml")["push"] == {"branches": ["main"]}
    assert document["concurrency"] == {
        "group": "ci-${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": True,
    }
    assert set(jobs) == RELEASE_TESTED_JOBS | {"sonar", "ci_result", "release_start", "pr_tested"}
    for identifier in RELEASE_TESTED_JOBS:
        assert "if" not in jobs[identifier], identifier
        assert "continue-on-error" not in jobs[identifier], identifier
    assert "push" not in jobs["sonar"]["if"]
    assert "github.ref" not in jobs["sonar"]["if"]
    assert jobs["ci_result"]["if"] == "${{ !cancelled() }}"
    for job in jobs.values():
        assert "release_start" not in str(job.get("needs", "")), job["name"]
        assert "pr_tested" not in str(job.get("needs", "")), job["name"]


def test_a_passed_pull_request_run_records_its_tested_tree_in_a_job_without_repository_code() -> (
    None
):
    jobs = ci_jobs()
    record_job = jobs["pr_tested"]
    checkout, read, record = record_job["steps"]
    name = (
        record["with"]["name"]
        .replace("${{ github.event.pull_request.number }}", "82")
        .replace("${{ steps.tested.outputs.base }}", PARENT)
        .replace("${{ steps.tested.outputs.tree }}", TREE)
        .replace("${{ github.run_attempt }}", "2")
    )
    match = release_authorization.TESTED_TREE_ARTIFACT.fullmatch(name)

    assert record_job["name"] == release_authorization.TESTED_TREE_JOB
    assert set(record_job["needs"]) == {*jobs["ci_result"]["needs"], "ci_result"}
    assert record_job["if"] == (
        "${{ !cancelled() && github.event_name == 'pull_request' && "
        "needs.ci_result.result == 'success' }}"
    )
    assert record_job["continue-on-error"] is True
    assert "permissions" not in record_job
    assert str(checkout["uses"]).startswith("actions/checkout@")
    assert checkout["with"] == {"persist-credentials": False}
    assert read["name"] == "Read the tree this pull request run tested"
    assert read["id"] == "tested"
    assert "if" not in read
    assert read["env"] == {"PULL_REQUEST_HEAD": "${{ github.event.pull_request.head.sha }}"}
    for fragment in ("python", "uv ", "scripts/", "deploy/", "npm", "npx", "bash ", "./", "sh "):
        assert fragment not in read["run"], fragment
    assert (
        "mapfile -t parents < <(git cat-file commit HEAD | sed -n '/^$/q; s/^parent //p')"
        in read["run"]
    )
    assert "git rev-parse 'HEAD^{tree}'" in read["run"]
    assert '"${parents[1]}" == "$PULL_REQUEST_HEAD"' in read["run"]
    assert record["name"] == "Record the tested tree for an early release of its squash merge"
    assert record["if"] == "steps.tested.outputs.tree != ''"
    assert str(record["uses"]).startswith("actions/upload-artifact@")
    assert record["with"]["if-no-files-found"] == "error"
    assert record["with"]["retention-days"] == 7
    assert match
    assert (match["number"], match["base"], match["tree"], match["attempt"]) == (
        "82",
        PARENT,
        TREE,
        "2",
    )


def test_the_release_judges_a_ci_run_its_dispatch_names_through_the_same_gate() -> None:
    inputs = triggers("release.yml")["workflow_dispatch"]["inputs"]
    authorize = release_jobs()["authorize"]
    collect = step(authorize, "Collect the CI run of the current main commit")
    script = " ".join(collect["with"]["script"].split())

    assert set(inputs) == {"ci_run_id", "ci_run_attempt"}
    for value in inputs.values():
        assert (value["required"], value["type"]) == (False, "string")
    assert (
        "const requested = context.eventName === 'workflow_dispatch' && inputs.ci_run_id "
        "? { id: inputs.ci_run_id, run_attempt: inputs.ci_run_attempt ?? '' } : null;"
    ) in script
    assert "github.rest.repos.listPullRequestsAssociatedWithCommit" in script
    assert (
        "const runs = await github.paginate(github.rest.actions.listWorkflowRuns, { ...repository, "
        "workflow_id: workflow.id, event: 'pull_request', head_sha: merged[0].head.sha, "
        "per_page: 100, });"
    ) in script
    assert "data.workflow_runs.reduce" not in script
    assert (
        ".map(job => ({ name: job.name, conclusion: job.conclusion, "
        "started_at: job.started_at, completed_at: job.completed_at }))"
    ) in script
    assert (
        "head_sha: artifact.workflow_run?.head_sha ?? null, created_at: artifact.created_at,"
        in (script)
    )
    assert "if (requested) {" in script
    assert collect["env"] == {"SONAR_ENABLED": "${{ vars.SONAR_ENABLED }}"}
    assert "sonar_enabled: process.env.SONAR_ENABLED === 'true'," in script
    assert "if (!requested) throw error; runError = error.message;" in script
    assert "run_error: runError," in script
    assert authorize["permissions"] == {
        "contents": "read",
        "actions": "read",
        "deployments": "read",
        "pull-requests": "read",
    }
    assert authorize["outputs"]["ci_started_at"] == "${{ steps.decision.outputs.ci_started_at }}"
    assert authorize["outputs"]["proven"] == "${{ steps.decision.outputs.proven }}"


def test_an_early_start_is_classified_like_the_release_after_the_whole_run() -> None:
    classify = step(release_jobs()["authorize"], "Classify the changes since the deployed commit")

    assert classify["env"]["RELEASE_EVENT"] == (
        "${{ inputs.ci_run_id != '' && 'workflow_run' || github.event_name }}"
    )
    assert classify["run"] == (
        'python3 deploy/release_scope.py "$RELEASE_EVENT" "$DEPLOYED_SHA" "$RELEASE_SHA"'
    )


def deployment_lookups() -> list[tuple[str, dict[str, Any]]]:
    return [
        (identifier, candidate)
        for identifier, job in release_jobs().items()
        for candidate in job["steps"]
        if str(candidate.get("name", "")).startswith(FIND_DEPLOYMENTS)
    ]


def test_every_release_reads_the_deployments_of_its_commit_with_every_status_and_its_id() -> None:
    lookups = deployment_lookups()
    scripts = {" ".join(candidate["with"]["script"].split()) for _, candidate in lookups}
    (script,) = scripts
    collect = step(release_jobs()["authorize"], "Collect the CI run of the current main commit")
    collected = " ".join(collect["with"]["script"].split())
    kept = "statuses: statuses.map(({ id, state, created_at }) => ({ id, state, created_at }))"

    assert [identifier for identifier, _ in lookups] == ["scan", "scan", "deploy", "deploy"]
    for _, candidate in lookups:
        assert candidate["continue-on-error"] is True
        assert candidate["timeout-minutes"] == 1
    assert script.startswith(
        "const fs = require('fs'); "
        "const path = `${process.env.RUNNER_TEMP}/release-deployments.json`; "
        "fs.rmSync(path, { force: true });"
    )
    assert "environment: 'production', sha: process.env.RELEASE_SHA" in script
    assert kept in script
    assert kept in collected
    assert "fs.writeFileSync(path, JSON.stringify(records));" in script
    assert "Date.parse" not in script
    assert "Date.parse" not in collected


def test_the_deployed_baseline_takes_the_newest_deployment_and_status_by_time_then_by_id() -> None:
    deployed = step(
        release_jobs()["authorize"],
        "Find the commit of the newest production deployment when it succeeded",
    )
    script = " ".join(deployed["with"]["script"].split())

    assert (
        "const newest = records => records.reduce((latest, record) => { "
        "const later = Date.parse(record.created_at) - Date.parse(latest.created_at); "
        "return later > 0 || (later === 0 && record.id > latest.id) ? record : latest; });"
    ) in script


def test_an_automatic_release_transfers_nothing_for_a_commit_released_since_its_ci_run() -> None:
    deploy = release_jobs()["deploy"]
    find = step(deploy, FIND_DEPLOYMENTS)
    current = step(deploy, "Decide whether main has moved past this release")

    assert find["if"] == AUTOMATIC
    assert deploy["steps"].index(find) < deploy["steps"].index(current)
    assert step(deploy, FIND_BEFORE_DOWNLOAD)["if"] == AUTOMATIC
    assert deploy["permissions"] == {
        "contents": "read",
        "actions": "write",
        "id-token": "write",
        "deployments": "read",
    }


NAMED_FILE = re.compile(
    r"(?<![\w$}./-])(?:[\w.-]+/)*[\w.-]+\.(?:py|sh|mjs|ts|json|toml|lock|properties|tf|hcl|ya?ml)\b"
)


def test_every_repository_file_a_workflow_step_names_is_a_pipeline_path() -> None:
    named: set[str] = set()
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for job in workflow(path.name)["jobs"].values():
            for candidate in job["steps"]:
                options = candidate.get("with", {})
                text = f"{candidate.get('run', '')}\n{options}"
                named.update(NAMED_FILE.findall(text))
                if "file" in options:
                    named.add(options["file"])
    tracked = {name for name in named if (ROOT / name).is_file()}

    assert {
        "Dockerfile",
        "web/Dockerfile",
        "web/package-lock.json",
        "scripts/check.py",
        "scripts/ci_result.py",
        "scripts/check_scientific.py",
        "deploy/release_authorization.py",
        "deploy/scan-images.sh",
    } <= tracked
    for name in sorted(tracked):
        assert release_authorization.changes_pipeline(name), name


def test_no_workflow_runs_for_pull_request_targets_and_every_action_is_pinned_by_commit() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        assert "pull_request_target" not in triggers(path.name), path.name
        for job in workflow(path.name)["jobs"].values():
            for candidate in job["steps"]:
                if "uses" in candidate:
                    assert PINNED_ACTION.fullmatch(str(candidate["uses"])), candidate["uses"]


def test_in_ci_only_the_release_start_may_write_and_only_to_actions() -> None:
    writers = {
        identifier: job["permissions"]
        for identifier, job in ci_jobs().items()
        if "write" in str(job.get("permissions", {}))
    }

    assert workflow("ci.yml")["permissions"] == {"contents": "read"}
    assert writers == {"release_start": {"actions": "write"}}
