import os
import shutil
import subprocess
from pathlib import Path

import pytest

from deploy import release_authorization
from scripts import ci_plan
from tests.support.workflows import ci_jobs

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")
README = "README.md"


def git(root: Path, *arguments: str) -> str:
    assert GIT is not None
    identity = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")
    # GIT_DIR and similar variables exported to hooks would redirect these commands into the
    # repository that runs the tests.
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    result = subprocess.run(  # noqa: S603 -- Resolved git executable on a test repository.
        [GIT, "-C", str(root), *identity, "-c", "commit.gpgsign=false", *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


def change(root: Path, *paths: str) -> None:
    for path in paths:
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("a", encoding="utf-8") as stream:
            stream.write("change\n")
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "--message", "change")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "--quiet", "--initial-branch=main")
    change(root, README, *sorted(ci_plan.AUDIT_INPUTS))
    git(root, "branch", "feature")
    return root


def merge_checkout(root: Path, tmp_path: Path) -> Path:
    git(root, "checkout", "--quiet", "-b", "merge", "main")
    git(root, "merge", "--quiet", "--no-ff", "--message", "merge", "feature")
    clone = tmp_path / "checkout"
    git(
        tmp_path, "clone", "--quiet", "--depth", "2", "--branch", "merge", root.as_uri(), "checkout"
    )
    return clone


def on_feature(root: Path) -> Path:
    git(root, "checkout", "--quiet", "feature")
    return root


def test_a_pull_request_that_changes_no_audit_input_owes_no_audit(
    repository: Path, tmp_path: Path
) -> None:
    change(on_feature(repository), README, "src/pyracast/views.py")
    changed = ci_plan.merged_changes(merge_checkout(repository, tmp_path))

    assert sorted(changed) == [README, "src/pyracast/views.py"]
    assert ci_plan.audit_owed("pull_request", changed) is False


@pytest.mark.parametrize("path", sorted(ci_plan.AUDIT_INPUTS))
def test_a_pull_request_that_changes_an_audit_input_owes_the_audit(
    repository: Path, tmp_path: Path, path: str
) -> None:
    change(on_feature(repository), path)
    changed = ci_plan.merged_changes(merge_checkout(repository, tmp_path))

    assert changed == [path]
    assert ci_plan.audit_owed("pull_request", changed) is True


def test_moving_an_audit_input_away_owes_the_audit(repository: Path, tmp_path: Path) -> None:
    on_feature(repository)
    git(repository, "mv", "web/audit-exceptions.json", "web/exceptions.json")
    git(repository, "commit", "--quiet", "--message", "move")
    changed = ci_plan.merged_changes(merge_checkout(repository, tmp_path))

    assert sorted(changed) == ["web/audit-exceptions.json", "web/exceptions.json"]
    assert ci_plan.audit_owed("pull_request", changed) is True


def test_only_the_changes_of_the_pull_request_count_not_those_of_its_base(
    repository: Path, tmp_path: Path
) -> None:
    change(repository, "uv.lock")
    change(on_feature(repository), README)

    assert ci_plan.merged_changes(merge_checkout(repository, tmp_path)) == [README]


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule"])
def test_every_run_but_a_pull_request_owes_the_audit(event: str) -> None:
    assert ci_plan.audit_owed(event, []) is True


def test_main_prints_the_decision_for_the_step_output(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    change(on_feature(repository), README)
    monkeypatch.chdir(merge_checkout(repository, tmp_path))

    assert ci_plan.main({"GITHUB_EVENT_NAME": "pull_request"}) == 0
    assert capsys.readouterr().out == "audit=false\n"
    assert ci_plan.main({"GITHUB_EVENT_NAME": "push"}) == 0
    assert capsys.readouterr().out == "audit=true\n"


def test_main_fails_a_pull_request_run_whose_base_it_cannot_read(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repository)

    with pytest.raises(subprocess.CalledProcessError):
        ci_plan.main({"GITHUB_EVENT_NAME": "pull_request"})


AUDIT_COMMANDS = (
    "uv sync --locked --no-build\n"
    "uv export --locked --all-groups --all-extras --no-emit-project --no-hashes"
    " --output-file audit-requirements.txt\n"
    "uv run --locked --no-sync --no-build pip-audit --strict --no-deps --disable-pip"
    " --requirement audit-requirements.txt\n"
    "uv run --locked --no-sync --no-build python scripts/npm_audit.py web"
)


def test_the_audit_inputs_are_what_the_audit_commands_read() -> None:
    audit = ci_jobs()["audit"]
    npm_audit = (ROOT / "scripts" / "npm_audit.py").read_text(encoding="utf-8")

    assert "\n".join(step["run"] for step in audit["steps"] if "run" in step) == AUDIT_COMMANDS
    assert '"audit-exceptions.json"' in npm_audit
    read_by_the_audit = {
        "uv.lock",
        "pyproject.toml",
        ".python-version",
        "web/package.json",
        "web/package-lock.json",
        "web/audit-exceptions.json",
        "scripts/npm_audit.py",
        ".github/workflows/ci.yml",
        ".github/workflows/dependency-audit.yml",
    }
    assert read_by_the_audit == ci_plan.AUDIT_INPUTS
    for path in ci_plan.AUDIT_INPUTS:
        assert (ROOT / path).is_file(), path


def test_every_audit_input_keeps_a_squash_that_changes_it_from_an_early_release() -> None:
    for path in ci_plan.AUDIT_INPUTS:
        assert release_authorization.changes_pipeline(path), path
