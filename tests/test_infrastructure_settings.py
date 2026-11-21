import json
import re
from pathlib import Path

import pytest

from deploy import image_rescan
from tests.support.workflows import ci_jobs, step, step_index

ROOT = Path(__file__).resolve().parents[1]
TERRAFORM = ROOT / "infra" / "terraform"
SETTING = {
    "provider": "projects/123456789/locations/global/workloadIdentityPools/pyracast-rescan/"
    "providers/github",
    "service_account": "pyracast-rescan@pyracast-production.iam.gserviceaccount.com",
    "repository": "europe-west1-docker.pkg.dev/pyracast-production/pyracast",
}


def terraform(name: str) -> str:
    return (TERRAFORM / name).read_text(encoding="utf-8")


def blocks(name: str) -> dict[str, str]:
    found = {}
    for match in re.finditer(
        r'^(resource|output|variable) ("[^"]+"(?: "[^"]+")?) \{\n(.*?)\n\}$',
        terraform(name),
        re.M | re.S,
    ):
        found[f"{match[1]} {match[2]}"] = " ".join(match[3].split())
    return found


def test_the_rescan_setting_is_read_into_the_three_values_the_workflow_uses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert image_rescan.main([json.dumps(SETTING)]) == 0

    assert capsys.readouterr().out.splitlines() == [
        f"{key}={value}" for key, value in SETTING.items()
    ]


@pytest.mark.parametrize(
    "setting",
    [
        "not json",
        json.dumps([SETTING]),
        json.dumps({**SETTING, "provider": "projects/x/providers/github"}),
        json.dumps({**SETTING, "service_account": "reader@example.com"}),
        json.dumps({**SETTING, "repository": "docker.io/library/postgres"}),
        json.dumps({**SETTING, "repository": SETTING["repository"] + "\nREPOSITORY=x"}),
        json.dumps({key: value for key, value in SETTING.items() if key != "repository"}),
        json.dumps({**SETTING, "extra": "value"}),
    ],
)
def test_a_rescan_setting_that_names_no_valid_reader_is_refused_in_one_line(
    capsys: pytest.CaptureFixture[str], setting: str
) -> None:
    assert image_rescan.main([setting]) == 2

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (
        "GCP_IMAGE_RESCAN must be the image_rescan_setting output of Terraform: a JSON object "
        "of provider, service_account and repository.\n"
    )


def test_the_registry_reader_is_off_until_the_operator_turns_it_on() -> None:
    variable = blocks("variables.tf")['variable "image_rescan_reader"']
    rescan = {name: body for name, body in blocks("iam.tf").items() if "rescan" in name}

    assert "type = bool" in variable
    assert "default = false" in variable
    assert set(rescan) == {
        'resource "google_iam_workload_identity_pool" "rescan"',
        'resource "google_iam_workload_identity_pool_provider" "rescan"',
        'resource "google_service_account" "rescan"',
        'resource "google_service_account_iam_member" "rescan_workflow"',
        'resource "google_artifact_registry_repository_iam_member" "rescan_read"',
    }
    assert all(
        body.startswith("count = var.image_rescan_reader ? 1 : 0 ") for body in rescan.values()
    )


def test_only_the_rescan_workflow_on_main_can_act_as_the_reader_and_it_only_reads_images() -> None:
    iam = blocks("iam.tf")
    provider = iam['resource "google_iam_workload_identity_pool_provider" "rescan"']
    workflow = iam['resource "google_service_account_iam_member" "rescan_workflow"']
    read = iam['resource "google_artifact_registry_repository_iam_member" "rescan_read"']
    release_pool = iam['resource "google_iam_workload_identity_pool_provider" "github"']

    assert "workload_identity_pool_id = google_iam_workload_identity_pool.rescan[0]" in provider
    assert "assertion.ref == 'refs/heads/main'" in provider
    assert (
        "assertion.workflow_ref == '${var.github_repository}/.github/workflows/image-rescan.yml"
        "@refs/heads/main'"
    ) in provider
    assert "assertion.repository_id == '${var.github_repository_id}'" in provider
    assert "image-rescan" not in release_pool
    assert 'role = "roles/iam.workloadIdentityUser"' in workflow
    assert "google_iam_workload_identity_pool.rescan[0].name" in workflow
    assert 'role = "roles/artifactregistry.reader"' in read
    assert "member = google_service_account.rescan[0].member" in read
    others = {name: body for name, body in iam.items() if "rescan" not in name}
    assert not any("google_service_account.rescan" in body for body in others.values())


def test_the_rescan_setting_output_is_null_until_the_reader_exists() -> None:
    output = blocks("outputs.tf")['output "image_rescan_setting"']

    assert "value = var.image_rescan_reader ? jsonencode({" in output
    for key in SETTING:
        assert f"{key} = " in output
    assert output.endswith("}) : null")


def test_every_read_of_a_secret_reaches_the_data_access_audit_log() -> None:
    audit = blocks("iam.tf")['resource "google_project_iam_audit_config" "secret_manager"']

    assert audit == (
        'project = var.project_id service = "secretmanager.googleapis.com" '
        'audit_log_config { log_type = "DATA_READ" }'
    )


@pytest.mark.parametrize("name", ["github_repository_id", "github_owner_id"])
def test_the_github_identities_that_bind_the_release_are_numeric(name: str) -> None:
    variable = blocks("variables.tf")[f'variable "{name}"']

    assert f'validation {{ condition = can(regex("^[0-9]+$", var.{name}))' in variable


def test_the_state_backend_example_uses_the_documented_prefix() -> None:
    assert 'prefix = "production"' in terraform("backend.hcl.example")
    assert "-backend-config=prefix=production" in (ROOT / "docs" / "DEPLOYMENT.md").read_text(
        encoding="utf-8"
    )


def test_ci_runs_the_boot_script_twice_and_more_on_a_stubbed_host() -> None:
    deployment = ci_jobs()["deployment"]
    boot = step(deployment, "Verify the boot script on a stubbed host without cloud access")
    harness = (ROOT / "deploy" / "test-boot.sh").read_text(encoding="utf-8")

    assert boot["run"] == (
        "docker run --rm --network none --entrypoint bash "
        '-v "$PWD/deploy:/source:ro" -v "$PWD/infra/terraform/templates:/templates:ro" '
        "python:3.15.2-slim-trixie@sha256:"
        "65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 "
        "/source/test-boot.sh"
    )
    assert deployment["steps"].index(boot) < step_index(deployment, "terraform validate")
    assert re.findall(r"^echo 'Boot regression passed: (.*)'$", harness, re.M) == [
        "first boot",
        "a boot with a release installs its units",
        "unchanged boot",
        "changed host configuration",
        "upgrade of a host the earlier script set up",
        "Docker restart under the release lock",
        "a package change waits for the release lock",
        "a stopped boot applies the network guard on its retry",
        "a repository keyring without exactly the published key is refused",
        "a used disk is refused",
        "an unsupported host is refused",
    ]
