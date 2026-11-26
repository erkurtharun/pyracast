import re
from pathlib import PurePosixPath
from typing import Any

from deploy import release_authorization
from tests.support.workflows import WORKFLOWS, ci_jobs, release_jobs, step, workflow

SONAR = WORKFLOWS.parents[1] / "sonar-project.properties"
UPLOAD = "actions/upload-artifact@"
DOWNLOAD = "actions/download-artifact@"
REPORTS = ("pytest-results-", "playwright-results-", "playwright-production-results-")
GUARD = "Require the artifacts the producing jobs uploaded"
NEEDS_OUTPUT = re.compile(r"\$\{\{ needs\.(?P<job>[\w-]+)\.outputs\.(?P<key>[\w-]+) \}\}")
ENV = re.compile(r"\$\{\{ env\.(?P<name>\w+) \}\}")
UPLOADED_ID = re.compile(r"steps\.(?P<step>[\w-]+)\.outputs\.artifact-id")
SHA = "a" * 40
RUNNER_OS = {"ubuntu-latest": "Linux", "windows-latest": "Windows"}


def uses(candidate: dict[str, Any], action: str) -> bool:
    return str(candidate.get("uses", "")).startswith(action)


def runs_on_its_runner(job: dict[str, Any], candidate: dict[str, Any]) -> bool:
    match = re.fullmatch(r"runner\.os == '(\w+)'", str(candidate.get("if", "")))
    return match is None or RUNNER_OS[job["runs-on"]] == match[1]


def uploads() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path.name, identifier, candidate)
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for identifier, job in workflow(path.name)["jobs"].items()
        for candidate in job["steps"]
        if uses(candidate, UPLOAD) and runs_on_its_runner(job, candidate)
    ]


def downloads(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [candidate for candidate in job["steps"] if uses(candidate, DOWNLOAD)]


def needed(job: dict[str, Any]) -> list[str]:
    value = job.get("needs", [])
    return [value] if isinstance(value, str) else list(value)


def artifact_references(job: dict[str, Any], download: dict[str, Any]) -> list[str]:
    match = ENV.fullmatch(str(download["with"].get("artifact-ids", "")))
    return str(job.get("env", {}).get(match["name"], "")).split(",") if match else []


def upload_behind(producer: dict[str, Any], key: str) -> dict[str, Any]:
    (identifier,) = [match["step"] for match in UPLOADED_ID.finditer(producer["outputs"][key])]
    steps: list[dict[str, Any]] = producer["steps"]
    (upload,) = [candidate for candidate in steps if candidate.get("id") == identifier]
    assert uses(upload, UPLOAD)
    return upload


def outputs_read_by_downloads() -> set[tuple[str, str]]:
    read: set[tuple[str, str]] = set()
    for job in ci_jobs().values():
        for download in downloads(job):
            for reference in artifact_references(job, download):
                if match := NEEDS_OUTPUT.fullmatch(reference):
                    read.add((match["job"], match["key"]))
    return read


def test_every_uploaded_artifact_has_a_consumer_or_is_a_report() -> None:
    read = outputs_read_by_downloads()

    for source, identifier, upload in uploads():
        name = upload["with"]["name"]
        if name.startswith(REPORTS):
            assert "id" not in upload, name
        elif name.startswith("release-images-"):
            assert (source, identifier) == ("ci.yml", "deployment")
        elif name.startswith("pr-tested-"):
            assert (source, identifier) == ("ci.yml", "pr_tested")
        else:
            outputs = workflow(source)["jobs"][identifier].get("outputs", {})
            keys = {
                key
                for key, value in outputs.items()
                if f"steps.{upload.get('id')}.outputs.artifact-id" in value
            }
            assert keys, name
            assert {(identifier, key) for key in keys} <= read, name


def test_every_ci_download_reads_the_artifact_ids_its_producers_uploaded() -> None:
    jobs = ci_jobs()
    consumers = {identifier: job for identifier, job in jobs.items() if downloads(job)}

    assert set(consumers) == {"sonar"}
    for identifier, job in consumers.items():
        for download in downloads(job):
            options = download["with"]
            assert set(options) - {"path"} == {
                "artifact-ids",
                "github-token",
                "run-id",
                "digest-mismatch",
            }, identifier
            assert options["github-token"] == "${{ github.token }}"
            assert options["run-id"] == "${{ github.run_id }}"
            assert options["digest-mismatch"] == "error"
            variable = ENV.fullmatch(options["artifact-ids"])
            assert variable, identifier
            references = artifact_references(job, download)
            for reference in references:
                match = NEEDS_OUTPUT.fullmatch(reference)
                assert match, reference
                assert match["job"] in needed(job), reference
                upload_behind(jobs[match["job"]], match["key"])
            more = f"(,[0-9]+){{{len(references) - 1}}}" if len(references) > 1 else ""
            guard = step(job, GUARD)
            assert f"[[ ${variable['name']} =~ ^[0-9]+{more}$ ]]" in guard["run"].splitlines()
            assert job["steps"].index(guard) < job["steps"].index(download)
        assert job.get("permissions") == {"contents": "read", "actions": "read"}, identifier


def test_each_sonar_report_path_receives_the_report_its_producer_uploaded() -> None:
    jobs = ci_jobs()
    sonar = jobs["sonar"]
    properties = dict(
        line.split("=", 1) for line in SONAR.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    delivered: dict[str, str] = {}
    for download in downloads(sonar):
        (reference,) = artifact_references(sonar, download)
        match = NEEDS_OUTPUT.fullmatch(reference)
        assert match, reference
        report = PurePosixPath(upload_behind(jobs[match["job"]], match["key"])["with"]["path"])
        destination = PurePosixPath(download["with"].get("path", ".")) / report.name
        delivered[str(destination)] = str(report)

    assert delivered == {
        properties[key]: properties[key]
        for key in ("sonar.python.coverage.reportPaths", "sonar.javascript.lcov.reportPaths")
    }


def test_no_consumer_derives_an_artifact_from_the_run_attempt() -> None:
    for identifier, job in ci_jobs().items():
        if downloads(job):
            assert "github.run_attempt" not in str(job), identifier


def test_the_release_reads_the_attempt_from_the_image_artifact_name_ci_uploads() -> None:
    (upload,) = [candidate for candidate in uploads() if candidate[1] == "deployment"]
    name = (
        upload[2]["with"]["name"]
        .replace("${{ github.sha }}", SHA)
        .replace("${{ github.run_attempt }}", "3")
    )
    match = release_authorization.IMAGE_ARTIFACT.fullmatch(name)

    assert match
    assert (match["sha"], match["attempt"]) == (SHA, "3")


def test_the_release_downloads_and_verifies_the_images_its_authorization_selected() -> None:
    jobs = release_jobs()

    for identifier in ("scan", "deploy"):
        (download,) = downloads(jobs[identifier])
        (verify,) = [
            candidate
            for candidate in jobs[identifier]["steps"]
            if "deploy/ci_images.py verify" in candidate.get("run", "")
        ]
        assert download["with"]["artifact-ids"] == "${{ needs.authorize.outputs.artifact_id }}"
        assert download["with"]["run-id"] == "${{ needs.authorize.outputs.ci_run_id }}"
        assert verify["env"] == {
            "CI_RUN_ID": "${{ needs.authorize.outputs.ci_run_id }}",
            "CI_RUN_ATTEMPT": "${{ needs.authorize.outputs.ci_run_attempt }}",
        }
        assert "github.run_attempt" not in str(jobs[identifier])


def test_the_release_judges_the_ci_attempt_that_triggered_it() -> None:
    collect = step(release_jobs()["authorize"], "Collect the CI run of the current main commit")
    script = " ".join(collect["with"]["script"].split())

    assert (
        "const source = requested ?? (context.eventName === 'workflow_run' "
        "? context.payload.workflow_run : null);"
    ) in script
    assert (
        "github.rest.actions.getWorkflowRunAttempt({ ...repository, "
        "run_id: Number(source.id), attempt_number: Number(source.run_attempt), })"
    ) in script
    assert (
        "github.paginate(github.rest.actions.listJobsForWorkflowRunAttempt, { ...repository, "
        "run_id: candidate.id, attempt_number: candidate.run_attempt, per_page: 100, })"
    ) in script
    assert "getWorkflowRun(" not in script
    assert "listJobsForWorkflowRun," not in script
