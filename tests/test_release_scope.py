import functools
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from deploy import release_scope
from tests.support.workflows import WORKFLOWS, workflow_files

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")
RUN = "workflow_run"
GLOB_CHARACTERS = "*?["


def _git(root: Path, *arguments: str) -> str:
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


def _commit(root: Path, *paths: str) -> str:
    for path in paths:
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("a", encoding="utf-8") as stream:
            stream.write("change\n")
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "--message", "change")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "--quiet")
    return tmp_path


def test_only_unshipped_paths_since_the_deployed_commit_skip_the_release(repository: Path) -> None:
    deployed = _commit(repository, "src/app.py", "docs/a.md")
    _commit(repository, "docs/a.md", "docs/flows/b.md")
    head = _commit(repository, "tests/test_a.py")

    decision = release_scope.decide(RUN, deployed, head, repository)

    assert decision.release is False
    assert decision.reason == (
        f"Nothing to release: changes since {deployed} touch only unshipped paths."
    )


def test_a_documentation_commit_after_an_undeployed_runtime_commit_releases(
    repository: Path,
) -> None:
    deployed = _commit(repository, "src/app.py")
    _commit(repository, "src/app.py")
    head = _commit(repository, "docs/a.md")

    decision = release_scope.decide(RUN, deployed, head, repository)

    assert decision.release is True
    assert "src/app.py" in decision.reason


@pytest.mark.parametrize(
    ("baseline", "reason"),
    [
        ("missing", "No usable deployed commit was found."),
        ("short", "No usable deployed commit was found."),
        ("unknown", "Git could not compare the commits"),
        ("diverged", "is not an ancestor"),
    ],
)
def test_missing_unknown_or_non_ancestor_baseline_releases(
    repository: Path, baseline: str, reason: str
) -> None:
    _commit(repository, "src/app.py")
    _git(repository, "checkout", "--quiet", "-b", "side")
    side = _commit(repository, "docs/side.md")
    _git(repository, "checkout", "--quiet", "-")
    head = _commit(repository, "docs/a.md")
    deployed = {"missing": "", "short": side[:12], "unknown": "f" * 40, "diverged": side}[baseline]

    decision = release_scope.decide(RUN, deployed, head, repository)

    assert decision.release is True
    assert reason in decision.reason


@pytest.mark.parametrize("head", ["HEAD", "upper-case"])
def test_an_authorized_commit_that_is_not_a_full_sha_releases(repository: Path, head: str) -> None:
    deployed = _commit(repository, "src/app.py")
    full = _commit(repository, "docs/a.md")
    authorized = full.upper() if head == "upper-case" else head

    decision = release_scope.decide(RUN, deployed, authorized, repository)

    assert decision == release_scope.Decision(True, "The authorized commit is not a full SHA.")


def test_an_unchanged_deployed_commit_releases_again(repository: Path) -> None:
    head = _commit(repository, "docs/a.md")

    assert release_scope.decide(RUN, head, head, repository).release is True


@pytest.mark.parametrize("failure", ["not-a-directory", "no-git-executable", "diff-exit-status"])
def test_git_failure_releases(
    repository: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    deployed = _commit(repository, "src/app.py")
    head = _commit(repository, "docs/a.md")
    root = repository
    if failure == "not-a-directory":
        root = repository / "missing"
    elif failure == "no-git-executable":
        monkeypatch.setattr(shutil, "which", lambda _name: None)
    else:
        git = release_scope._git

        def failing_diff(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
            if arguments[0] == "diff":
                return subprocess.CompletedProcess(list(arguments), 1, b"docs/a.md\0", b"")
            return git(root, *arguments)

        monkeypatch.setattr(release_scope, "_git", failing_diff)

    decision = release_scope.decide(RUN, deployed, head, root)

    assert decision.release is True
    assert decision.reason.startswith("Git could not compare")


@pytest.mark.parametrize(
    "path",
    [
        "docs/a.md",
        "tests/test_b.py",
        "scripts/check.py",
        "scripts/__init__.py",
        ".agents/hooks.py",
        ".agents/skills/verify/SKILL.md",
        "AGENTS.md",
        "CLAUDE.md",
        ".claude/settings.json",
        ".codex/hooks.json",
        ".github/dependabot.yml",
        "qodana.yaml",
        "qodana-js.yaml",
        "sonar-project.properties",
        ".gitignore",
        "web/.gitignore",
        "web/.prettierignore",
        "web/tests/x.spec.ts",
        "web/tests/support/test.ts",
        "web/integration/x.spec.ts",
        "web/production-tests/x.spec.ts",
    ],
)
def test_each_unshipped_path_alone_skips_the_release(repository: Path, path: str) -> None:
    deployed = _commit(repository, "src/app.py")
    head = _commit(repository, path)

    decision = release_scope.decide(RUN, deployed, head, repository)

    assert decision.release is False


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "pyproject.toml",
        "uv.lock",
        "deploy/x.py",
        "deploy/postgres/Dockerfile",
        "deploy/rabbitmq/Dockerfile",
        "web/src/x.ts",
        ".github/workflows/ci.yml",
        "docsx/a.md",
        "Docs/a.md",
        ".dockerignore",
        "web/.dockerignore",
        "Dockerfile",
        "web/Dockerfile",
        "web/package.json",
        "web/package-lock.json",
        "infra/terraform/main.tf",
        "infra/terraform/.gitignore",
        ".github/workflows/release.yml",
        "AGENTS.md.bak",
        "AGENTS.md/x.py",
        "agents.md",
        "src/AGENTS.md",
        "web/CLAUDE.md",
        "CLAUDE.md~",
        ".agentsx/a.md",
        ".claude/settings.local.json",
        ".claude/hooks/x.py",
        ".codex/config.toml",
        ".github/dependabot.yaml",
        ".github/workflows/dependabot.yml",
        "qodana.yaml.d/x.yaml",
        "sonar-project.properties.bak",
        "scriptsx/a.py",
        "src/scripts/x.py",
        "web/scripts/x.mjs",
        ".gitignore/x",
        ".gitattributes",
        "deploy/.gitignore",
        "web/src/.gitignore",
        "web/.prettierrc",
        "web/tests",
        "web/testsx/a.ts",
        "web/src/tests/x.ts",
        "web/integration.ts",
        "web/testing/coverage-reporter.ts",
        "web/playwright.config.ts",
        "web/playwright.production.config.ts",
        "web/production-tests2/a.ts",
    ],
)
def test_any_path_off_the_allowlist_releases(repository: Path, path: str) -> None:
    deployed = _commit(repository, "tests/test_a.py")
    head = _commit(repository, "tests/test_a.py", path)

    decision = release_scope.decide(RUN, deployed, head, repository)

    assert decision.release is True
    assert path in decision.reason


def test_renames_count_both_paths(repository: Path) -> None:
    deployed = _commit(repository, "src/a.py", "src/b.py")
    (repository / "docs").mkdir()
    (repository / "lib").mkdir()
    _git(repository, "mv", "src/a.py", "docs/a.py")
    _git(repository, "mv", "src/b.py", "lib/b.py")
    _git(repository, "commit", "--quiet", "--message", "move")
    head = _git(repository, "rev-parse", "HEAD")

    decision = release_scope.decide(RUN, deployed, head, repository)

    assert decision.release is True
    assert decision.reason.startswith(f"3 changed paths since {deployed} lie outside")
    assert all(path in decision.reason for path in ("src/a.py", "src/b.py", "lib/b.py"))
    assert "docs/a.py" not in decision.reason


def test_an_ambient_git_environment_does_not_redirect_the_comparison(
    repository: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoy = tmp_path_factory.mktemp("decoy")
    _git(decoy, "init", "--quiet")
    deployed = _commit(repository, "src/app.py")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    head = _commit(repository, "docs/a.md")

    decision = release_scope.decide(RUN, deployed, head, repository)

    assert decision.release is False
    assert _git(decoy, "rev-list", "--all") == ""


IgnoreRules = list[tuple[bool, re.Pattern[str]]]


def _glob_regex(pattern: str) -> str:
    # Docker's .dockerignore syntax (moby patternmatcher): Go filepath.Match per segment; "**"
    # spans any segments, and is "(.*/)?" unless it ends the pattern, even inside a segment.
    assert "\\" not in pattern, pattern
    parts = [part for part in re.split(r"(\*\*/?|\*|\?|\[[^\]]*\])", pattern) if part]
    special = {"**/": "(?:.*/)?", "**": "(?:.*/)?", "*": "[^/]*", "?": "[^/]"}
    translated = [special.get(part, part if part[0] == "[" else re.escape(part)) for part in parts]
    if parts and parts[-1] == "**":
        translated[-1] = ".*"
    return "".join(translated)


def _ignore_rules(text: str) -> IgnoreRules:
    rules: IgnoreRules = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            # Docker trims the text after "!", applies Go's filepath.Clean, drops a leading "/".
            pattern = posixpath.normpath(line.removeprefix("!").strip()).lstrip("/")
            assert pattern not in ("", "."), line
            assert not pattern.startswith("!"), line
            assert ".." not in pattern.split("/"), line
            rules.append((line.startswith("!"), re.compile(_glob_regex(pattern))))
    return rules


def _context_rules(context: str) -> IgnoreRules | None:
    ignore_file = ROOT / context / ".dockerignore"
    return _ignore_rules(ignore_file.read_text(encoding="utf-8")) if ignore_file.exists() else None


def _in_image(rules: IgnoreRules | None, path: str) -> bool:
    # Like Docker, the last matching rule wins and a rule matching a parent directory applies.
    parts = path.split("/")
    candidates = ["/".join(parts[:count]) for count in range(1, len(parts) + 1)]
    shipped = True
    for include, pattern in rules or []:
        if any(pattern.fullmatch(candidate) for candidate in candidates):
            shipped = include
    return shipped


def _under(path: str, root: str) -> bool:
    return root in ("", ".") or path == root or path.startswith(root.rstrip("/") + "/")


def _unshipped_samples() -> list[str]:
    probes = [
        entry.removesuffix("**") + "probe" if entry.endswith("/**") else entry
        for entry in release_scope.ALLOWLIST
    ]
    tracked = _git(ROOT, "ls-files", "-z").split("\0")
    return probes + [path for path in tracked if path and release_scope.unshipped(path)]


def _shipped_samples(context: str, rules: IgnoreRules | None) -> list[str]:
    samples = [path for path in _unshipped_samples() if _under(path, context)]
    prefix = "" if context == "." else context + "/"
    return [path for path in samples if _in_image(rules, path.removeprefix(prefix))]


def _image_contexts() -> list[str]:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    return re.findall(r"^\s+context: (\S+)$", workflow, flags=re.MULTILINE)


def _bundle_roots() -> list[str]:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    archives = re.findall(r"git archive (.+)$", workflow, flags=re.MULTILINE)
    assert len(archives) == 1, archives
    arguments = shlex.split(archives[0])
    roots = arguments[arguments.index("$RELEASE_SHA") + 1 :]
    assert roots, archives
    return roots


def test_no_allowlisted_path_is_shipped() -> None:
    contexts = _image_contexts()
    assert {".", "web"} <= set(contexts)
    assert _in_image(_context_rules("."), "src/pyracast/settings.py")
    assert _in_image(_context_rules("."), "README.md")
    assert _in_image(_context_rules("web"), "src/app/page.tsx")
    for context in contexts:
        assert _shipped_samples(context, _context_rules(context)) == [], context
    for root in _bundle_roots():
        assert not any(character in root for character in GLOB_CHARACTERS), root
        assert [path for path in _unshipped_samples() if _under(path, root)] == [], root
    assert _unmodelled_ignore_files(_git(ROOT, "ls-files", "-z").split("\0"), contexts) == []


def _unmodelled_ignore_files(tracked: list[str], contexts: list[str]) -> list[str]:
    # BuildKit prefers "<Dockerfile>.dockerignore" over the context's .dockerignore.
    modelled = {
        ".dockerignore" if context == "." else f"{context}/.dockerignore" for context in contexts
    }
    return [path for path in tracked if path.endswith(".dockerignore") and path not in modelled]


@pytest.mark.parametrize("added", ["Dockerfile.dockerignore", "web/Dockerfile.dockerignore"])
def test_the_shipped_path_check_sees_a_dockerfile_specific_ignore_file(added: str) -> None:
    tracked = [*_git(ROOT, "ls-files", "-z").split("\0"), added]

    assert _unmodelled_ignore_files(tracked, _image_contexts()) == [added]


FORBIDDEN_ROOTS = (".github/workflows/", "deploy/", "infra/", "src/", "web/src/")
FORBIDDEN_NAMES = ("pyproject.toml", "package.json", "uv.lock", "package-lock.json")


def _forbidden(entry: str) -> bool:
    if entry.endswith("/**"):
        directory = entry.removesuffix("**")
        return any(
            root.startswith(directory) or directory.startswith(root) for root in FORBIDDEN_ROOTS
        )
    name = entry.rsplit("/", 1)[-1]
    build_file = name.startswith("Dockerfile") or name.endswith(
        (".dockerignore", ".lock", ".lock.hcl")
    )
    return build_file or name in FORBIDDEN_NAMES or entry.startswith(FORBIDDEN_ROOTS)


def test_the_allowlist_holds_only_plain_paths_outside_build_inputs() -> None:
    for entry in release_scope.ALLOWLIST:
        assert not any(character in entry.removesuffix("/**") for character in GLOB_CHARACTERS)
        assert not _forbidden(entry), entry


@pytest.mark.parametrize(
    "entry",
    [
        ".github/workflows/x.yml",
        ".github/**",
        "infra/terraform/.terraform.lock.hcl",
        "web/package-lock.json",
        "web/Dockerfile",
        "Dockerfile.dockerignore",
        "deploy/x/**",
        "web/**",
        "web/src/x.ts",
    ],
)
def test_the_allowlist_check_rejects_a_build_input(entry: str) -> None:
    assert _forbidden(entry)


@pytest.mark.parametrize(
    ("context", "removed", "added"),
    [
        (".", "", "!d*/**"),
        (".", "", "!**/*.md"),
        (".", "", "!t?sts/"),
        (".", "", "!*.md"),
        (".", "", "!.agents"),
        (".", "", "!scripts/[a-c]*.py"),
        (".", "", "! docs/"),
        (".", "", "!./scripts/"),
        (".", "", "!scripts/./check.py"),
        (".", "", "!./AGENTS.md"),
        (".", "", "!web//tests"),
        (".", "", "!src/../docs"),
        (".", "", "!docs/\ndocs/**[a-z]*"),
        (".", "**", ""),
        ("web", "", "!t*/**"),
        ("web", "", "!integration"),
        ("web", "", "!.*ignore"),
        ("web", "**", ""),
    ],
)
def test_the_shipped_path_check_sees_every_re_include(
    context: str, removed: str, added: str
) -> None:
    text = (ROOT / context / ".dockerignore").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line != removed] + [added]

    assert _shipped_samples(context, _ignore_rules("\n".join(lines))) != []


WORKFLOW_FILES = frozenset(
    {"ci.yml", "codeql.yml", "dependency-audit.yml", "image-rescan.yml", "release.yml"}
)
JOBS_THAT_NEED_EVERY_PATH = frozenset(
    {
        "ci.yml verify_ubuntu",
        "ci.yml web_ubuntu",
        "ci.yml web_production",
        "ci.yml scientific",
        "ci.yml audit",
        "ci.yml sonar",
        "ci.yml ci_result",
        "ci.yml pr_tested",
        "codeql.yml analyze",
        "dependency-audit.yml audit",
    }
)


@functools.cache
def _derived_patterns() -> list[str]:
    result = subprocess.run(  # noqa: S603 -- The running interpreter on a tracked script.
        [sys.executable, str(ROOT / "deploy" / "release_scope.py"), "sparse-patterns"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def _is_checkout(step: dict[str, Any]) -> bool:
    return str(step.get("uses", "")).lower().startswith("actions/checkout@")


def _is_reduced_checkout(step: dict[str, Any]) -> bool:
    options = step.get("with", {})
    return (
        _is_checkout(step)
        and "if" not in step
        and "continue-on-error" not in step
        and options.get("sparse-checkout-cone-mode") is False
        and str(options.get("sparse-checkout", "")).splitlines() == _derived_patterns()
    )


def _calls_another_workflow(job: dict[str, Any]) -> bool:
    return "uses" in job


def _checks_out_without_the_unshipped_paths(job: dict[str, Any]) -> bool:
    checkouts = [step for step in job.get("steps", []) if _is_checkout(step)]
    return (
        not _calls_another_workflow(job)
        and len(checkouts) <= 1
        and all(_is_reduced_checkout(step) for step in checkouts)
    )


def _jobs(directory: Path) -> dict[str, dict[str, Any]]:
    return {
        f"{path.name} {identifier}": job
        for path in workflow_files(directory)
        for identifier, job in yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"].items()
    }


def _unknown_workflow_files(directory: Path, known: frozenset[str]) -> list[str]:
    return [path.name for path in workflow_files(directory) if path.name not in known]


def _jobs_with_an_unreduced_checkout(
    directory: Path, needing_every_path: frozenset[str]
) -> list[str]:
    return [
        name
        for name, job in _jobs(directory).items()
        if name not in needing_every_path and not _checks_out_without_the_unshipped_paths(job)
    ]


def test_every_workflow_file_is_known() -> None:
    assert _unknown_workflow_files(WORKFLOWS, WORKFLOW_FILES) == []


def test_only_the_jobs_that_need_every_path_check_out_the_unshipped_paths() -> None:
    assert _jobs_with_an_unreduced_checkout(WORKFLOWS, JOBS_THAT_NEED_EVERY_PATH) == []
    assert _jobs(WORKFLOWS).keys() >= JOBS_THAT_NEED_EVERY_PATH


def _reduced_checkout_step() -> dict[str, Any]:
    patterns = "".join(f"{pattern}\n" for pattern in _derived_patterns())
    return {
        "uses": "actions/checkout@v7",
        "with": {"sparse-checkout-cone-mode": False, "sparse-checkout": patterns},
    }


def _job(steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {"runs-on": "ubuntu-latest", "steps": steps}


def _write_workflow(directory: Path, name: str, jobs: dict[str, dict[str, Any]]) -> None:
    document = {"on": "push", "jobs": jobs}
    (directory / name).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _reported_jobs(directory: Path, job: dict[str, Any]) -> list[str]:
    _write_workflow(directory, "release.yml", {"build": job})
    return _jobs_with_an_unreduced_checkout(directory, frozenset())


def test_a_job_with_two_reduced_checkouts_is_reported(tmp_path: Path) -> None:
    steps = [_reduced_checkout_step(), _reduced_checkout_step()]

    assert _reported_jobs(tmp_path, _job(steps)) == ["release.yml build"]


def test_a_reduced_checkout_under_a_condition_is_reported(tmp_path: Path) -> None:
    checkout = {**_reduced_checkout_step(), "if": "github.event_name == 'push'"}

    assert _reported_jobs(tmp_path, _job([checkout])) == ["release.yml build"]


def test_a_full_checkout_of_a_job_not_named_as_needing_every_path_is_reported(
    tmp_path: Path,
) -> None:
    steps = [{"uses": "actions/checkout@v7"}, {"run": "make images"}]

    assert _reported_jobs(tmp_path, _job(steps)) == ["release.yml build"]


def test_a_full_checkout_in_another_letter_case_is_reported(tmp_path: Path) -> None:
    steps = [{"uses": "Actions/Checkout@v7"}]

    assert _reported_jobs(tmp_path, _job(steps)) == ["release.yml build"]


def test_a_job_that_calls_another_workflow_is_reported(tmp_path: Path) -> None:
    job = {"uses": "other-owner/other-repository/.github/workflows/build.yml@v1"}

    assert _reported_jobs(tmp_path, job) == ["release.yml build"]


def test_a_checkout_with_other_patterns_is_reported(tmp_path: Path) -> None:
    checkout = _reduced_checkout_step()
    patterns = checkout["with"]["sparse-checkout"].splitlines()[:-1]
    checkout["with"]["sparse-checkout"] = "\n".join(patterns)

    assert _reported_jobs(tmp_path, _job([checkout])) == ["release.yml build"]


def test_a_workflow_file_the_case_does_not_know_is_reported(tmp_path: Path) -> None:
    _write_workflow(tmp_path, "release.yml", {})
    _write_workflow(tmp_path, "build.yaml", {})

    assert _unknown_workflow_files(tmp_path, frozenset({"release.yml"})) == ["build.yaml"]


def test_reduced_jobs_named_jobs_and_jobs_without_a_checkout_pass(tmp_path: Path) -> None:
    jobs = {
        "build": _job([_reduced_checkout_step(), {"run": "./deploy/build-images.sh"}]),
        "verify": _job([{"uses": "actions/checkout@v7"}, {"run": "pytest"}]),
        "analyse": {"uses": "./.github/workflows/codeql.yml"},
        "cleanup": _job([{"run": "echo done"}]),
    }
    _write_workflow(tmp_path, "ci.yml", jobs)
    named = frozenset({"ci.yml verify", "ci.yml analyse"})

    assert _jobs_with_an_unreduced_checkout(tmp_path, named) == []


def test_a_directory_entry_and_an_exact_entry_become_exclusions() -> None:
    patterns = release_scope.sparse_patterns(("web/tests/**", "AGENTS.md"))

    assert patterns == ["/*", "!/web/tests/", "!/AGENTS.md"]


def test_the_command_prints_the_patterns_of_the_allowlist(
    capsys: pytest.CaptureFixture[str],
) -> None:
    release_scope.main(["sparse-patterns"])

    assert capsys.readouterr().out.splitlines() == release_scope.sparse_patterns(
        release_scope.ALLOWLIST
    )


LOOK_ALIKES = (
    "README.md",
    "AGENTS.md.bak",
    "src/AGENTS.md",
    "web/CLAUDE.md",
    "docsx/a.md",
    "scriptsx/a.py",
    "src/scripts/x.py",
    "web/testsx/a.ts",
    "web/src/tests/x.ts",
    "web/integration.ts",
    "web/production-tests2/a.ts",
    ".agentsx/a.md",
    ".claude/settings.local.json",
    ".claude/hooks/x.py",
    ".codex/config.toml",
    ".github/dependabot.yaml",
    ".github/workflows/ci.yml",
    "deploy/.gitignore",
    "web/src/.gitignore",
    "qodana.yaml.d/x.yaml",
)


def _working_tree(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _reduced_clone(source: Path, destination: Path) -> None:
    head = _git(source, "rev-parse", "HEAD")
    _git(source, "clone", "--quiet", "--shared", "--no-checkout", str(source), str(destination))
    # What the checkout action does for sparse-checkout-cone-mode false.
    _git(destination, "config", "core.sparseCheckout", "true")
    sparse_file = destination / _git(destination, "rev-parse", "--git-path", "info/sparse-checkout")
    sparse_file.parent.mkdir(parents=True, exist_ok=True)
    with sparse_file.open("a", encoding="utf-8", newline="") as stream:
        stream.write("\n" + "\n".join(_derived_patterns()) + "\n")
    _git(destination, "checkout", "--quiet", "--detach", head)


def _shipped_tracked_files(root: Path) -> set[str]:
    tracked = _git(root, "ls-tree", "-r", "--name-only", "-z", "HEAD").split("\0")
    return {path for path in tracked if path and not release_scope.unshipped(path)}


def test_the_reduced_checkout_of_this_repository_holds_exactly_the_shipped_files(
    tmp_path: Path,
) -> None:
    _reduced_clone(ROOT, tmp_path / "clone")

    assert _working_tree(tmp_path / "clone") == _shipped_tracked_files(ROOT)


def test_the_reduced_checkout_keeps_the_look_alikes_of_the_unshipped_paths(
    repository: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    probes = [
        entry.removesuffix("**") + "nested/probe" if entry.endswith("/**") else entry
        for entry in release_scope.ALLOWLIST
    ]
    _commit(repository, *probes, *LOOK_ALIKES)
    clone = tmp_path_factory.mktemp("reduced") / "clone"

    _reduced_clone(repository, clone)

    assert _working_tree(clone) == set(LOOK_ALIKES)


def test_no_infrastructure_file_names_an_allowlisted_path() -> None:
    tracked = _git(ROOT, "ls-files", "-z").split("\0")
    inputs = {
        path: (ROOT / path).read_text(encoding="utf-8", errors="replace")
        for path in tracked
        if path.startswith("infra/")
    }

    assert "infra/terraform/templates/startup.sh.tftpl" in inputs
    for source, text in inputs.items():
        assert _named_entries(text) == [], source


def _named_entries(text: str) -> list[str]:
    # Step names and comment lines are prose; any other mention of an entry's name counts.
    prose = re.compile(r"\s*(#|(- )?name:)")
    code = "\n".join(line for line in text.splitlines() if not prose.match(line))
    named = []
    for entry in release_scope.ALLOWLIST:
        name = entry.removesuffix("/**").rsplit("/", 1)[-1]
        if re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", code, flags=re.IGNORECASE):
            named.append(entry)
    return named


@pytest.mark.parametrize(
    "line",
    [
        'gcloud compute scp --recurse scripts "$GCP_INSTANCE:$stage/"',
        "tar -czf bundle.tgz deploy docs",
        '-v "$root/tests:/tests:ro"',
        'cd "$root/scripts"',
        'rsync -a scripts/ "$host:/opt/pyracast/scripts/"',
        "run: uv run pytest tests -k smoke",
        'python -c "from scripts import check; check.main()"',
        "python3 scripts/x.py",
        "COPY AGENTS.md ./",
        "cat web/.prettierignore",
        'cp -r web/production-tests "$stage/"',
        "tar -C web -cf archive.tar production-tests",
        "COPY production-tests/ ./specs/",
    ],
)
def test_the_release_input_guard_sees_a_named_allowlisted_path(line: str) -> None:
    assert _named_entries(line) != []


@pytest.mark.parametrize(
    "line",
    [
        "      - name: Validate GitHub workflows and deployment scripts",
        "  name: Run the tests",
        "# https://cloud.google.com/iap/docs/using-tcp-forwarding",
        "    # The smoke tests run against the stack.",
        "RUN npm ci --ignore-scripts",
        "    environment: production",
        "ENV NODE_ENV=production HOSTNAME=0.0.0.0 PORT=3000",
    ],
)
def test_the_release_input_guard_ignores_names_and_comments(line: str) -> None:
    assert _named_entries(line) == []


@pytest.mark.parametrize("event", ["workflow_dispatch", "push", ""])
def test_manual_dispatch_always_releases(repository: Path, event: str) -> None:
    deployed = _commit(repository, "src/app.py")
    head = _commit(repository, "docs/a.md")

    decision = release_scope.decide(event, deployed, head, repository)

    assert decision.release is True


@pytest.mark.parametrize(
    ("changed", "expected"), [("docs/a.md", "release=false\n"), ("src/b.py", "release=true\n")]
)
def test_main_writes_the_decision_and_its_reason(
    repository: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
    expected: str,
) -> None:
    deployed = _commit(repository, "src/app.py")
    head = _commit(repository, changed)
    outputs = tmp_path_factory.mktemp("outputs")
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs / "output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(outputs / "summary"))
    monkeypatch.chdir(repository)

    release_scope.main(["classify", RUN, deployed, head])

    assert (outputs / "output").read_text(encoding="utf-8") == expected
    summary = (outputs / "summary").read_text(encoding="utf-8")
    assert summary == release_scope.decide(RUN, deployed, head, repository).reason + "\n"


def test_main_writes_the_decision_without_a_step_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    release_scope.main(["classify", "workflow_dispatch", "", ""])

    assert (tmp_path / "output").read_text(encoding="utf-8") == "release=true\n"
    assert capsys.readouterr().out == "Event 'workflow_dispatch' always releases.\n"
