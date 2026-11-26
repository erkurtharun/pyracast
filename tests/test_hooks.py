import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK_SOURCE = ROOT / ".agents" / "hooks.py"
HOOK_CONFIGURATION = ROOT / ".codex" / "hooks.json"


def test_hooks_leave_full_project_checks_to_the_explicit_quality_command() -> None:
    hooks = json.loads(HOOK_CONFIGURATION.read_text(encoding="utf-8"))["hooks"]
    assert "Stop" not in hooks


def test_both_clients_use_the_same_hook_logic_without_project_permission_overrides() -> None:
    codex = json.loads(HOOK_CONFIGURATION.read_text(encoding="utf-8"))
    claude = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert claude == codex
    assert set(claude) == {"hooks"}
    for groups in claude["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                assert "/.agents/hooks.py" in hook["command"]


@pytest.mark.parametrize(
    ("path", "ignored"),
    [
        (".claude/settings.local.json", True),
        (".claude/settings.json", False),
        (".mcp.json", True),
        ("CLAUDE.local.md", True),
    ],
)
def test_personal_agent_configuration_remains_ignored(path: str, ignored: bool) -> None:
    result = _run(["git", "check-ignore", "--no-index", path], ROOT)
    assert result.returncode == (0 if ignored else 1), result.stderr


def test_configured_hook_command_runs_from_a_nested_directory() -> None:
    hooks = json.loads(HOOK_CONFIGURATION.read_text(encoding="utf-8"))["hooks"]
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(ROOT / ".venv" / "pyvenv.cfg")}}
    result = subprocess.run(  # noqa: S602 -- execute the tracked hook command as the clients do.
        hooks["PreToolUse"][0]["hooks"][0]["command"],
        shell=True,
        cwd=ROOT / "tests",
        input=json.dumps(payload),
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_post_tool_preserves_registration_imports(repository: Path) -> None:
    edited = repository / "edited.py"
    edited.write_text("import registered_plugin\nvalues=[ 1,2 ]\n", encoding="utf-8")
    result = _invoke(
        repository, "post-tool", {"tool_name": "Write", "tool_input": {"file_path": str(edited)}}
    )
    assert result.returncode == 0, result.stderr
    assert edited.read_text(encoding="utf-8") == "import registered_plugin\n\nvalues = [1, 2]\n"


@pytest.mark.parametrize(
    "path", ["src/data/module.py", "tests/fixtures/data/input.json", "src/build/__init__.py"]
)
def test_read_guard_allows_source_packages_named_like_root_artifacts(
    repository: Path, path: str
) -> None:
    result = _invoke(
        repository, "pre-read", {"tool_name": "Read", "tool_input": {"file_path": path}}
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def _run(
    command: list[str],
    root: Path,
    *,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed executables, argument lists and isolated fixtures.
        command,
        cwd=root,
        input=stdin,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    hooks = tmp_path / ".agents"
    hooks.mkdir(parents=True)
    shutil.copyfile(HOOK_SOURCE, hooks / "hooks.py")
    return tmp_path


def _invoke(
    root: Path,
    event: str,
    payload: dict[str, object] | str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [sys.executable, str(root / ".agents" / "hooks.py"), event],
        root,
        stdin=payload if isinstance(payload, str) else json.dumps(payload),
        env=env,
    )


def _decision(result: subprocess.CompletedProcess[str]) -> dict[str, str] | None:
    assert result.returncode == 0, result.stderr
    if not result.stdout:
        return None
    output: dict[str, str] = json.loads(result.stdout)["hookSpecificOutput"]
    return output


@pytest.mark.parametrize("tool_name", ["Edit", "Write", "MultiEdit"])
def test_edit_formats_only_the_edited_python_file(repository: Path, tool_name: str) -> None:
    edited = repository / "edited file.py"
    edited.write_text("import os\nvalues=[ 1,2 ]\n", encoding="utf-8")
    untouched = repository / "unrelated.py"
    untouched.write_text("other=[ 3,4 ]\n", encoding="utf-8")

    result = _invoke(
        repository,
        "post-tool",
        {"tool_name": tool_name, "tool_input": {"file_path": str(edited)}},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert edited.read_text(encoding="utf-8") == "import os\n\nvalues = [1, 2]\n"
    assert untouched.read_text(encoding="utf-8") == "other=[ 3,4 ]\n"


def test_apply_patch_formats_added_and_moved_paths_and_ignores_deleted_paths(
    repository: Path,
) -> None:
    added = repository / "added ölçüm.py"
    moved = repository / "moved.py"
    for path in (added, moved):
        path.write_text("values=[ 1,2 ]\n", encoding="utf-8")
    payload: dict[str, object] = {
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n*** Add File: added ölçüm.py\n+values=[ 1,2 ]\n"
                "*** Update File: original.py\n*** Move to: moved.py\n@@\n"
                "-old\n+new\n*** Delete File: deleted.py\n*** End Patch"
            )
        },
        "tool_response": (
            "Success. Updated the following files:\nA added ölçüm.py\nM moved.py\nD deleted.py\n"
        ),
    }

    result = _invoke(repository, "post-tool", payload)

    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert added.read_text(encoding="utf-8") == "values = [1, 2]\n"
    assert moved.read_text(encoding="utf-8") == "values = [1, 2]\n"
    assert not (repository / "deleted.py").exists()


@pytest.mark.parametrize(
    "response",
    [
        "Success. Updated the following files:\nM response.py\n",
        {"output": "Success. Updated the following files:\nM response.py\n"},
        {
            "content": [
                {"type": "text", "text": "Success. Updated the following files:\nM response.py\n"}
            ]
        },
    ],
)
def test_apply_patch_accepts_model_facing_response_paths(
    repository: Path, response: object
) -> None:
    edited = repository / "response.py"
    edited.write_text("values=[ 1,2 ]\n", encoding="utf-8")

    result = _invoke(
        repository,
        "post-tool",
        {"tool_name": "apply_patch", "tool_input": {}, "tool_response": response},
    )

    assert result.returncode == 0, result.stderr
    assert edited.read_text(encoding="utf-8") == "values = [1, 2]\n"


@pytest.mark.parametrize("absolute", [False, True])
def test_post_tool_does_not_touch_paths_outside_the_repository(
    repository: Path, absolute: bool
) -> None:
    outside = repository.parent / f"outside-{repository.name}.py"
    outside.write_text("values=[ 1,2 ]\n", encoding="utf-8")
    path = str(outside) if absolute else f"../{outside.name}"

    result = _invoke(
        repository, "post-tool", {"tool_name": "Write", "tool_input": {"file_path": path}}
    )

    assert result.returncode == 0, result.stderr
    assert outside.read_text(encoding="utf-8") == "values=[ 1,2 ]\n"


def test_post_tool_resolves_relative_paths_from_session_cwd(repository: Path) -> None:
    nested = repository / "nested"
    nested.mkdir()
    edited = nested / "module.py"
    edited.write_text("values=[ 1,2 ]\n", encoding="utf-8")

    result = _invoke(
        repository,
        "post-tool",
        {"tool_name": "Write", "cwd": str(nested), "tool_input": {"file_path": "module.py"}},
    )

    assert result.returncode == 0, result.stderr
    assert edited.read_text(encoding="utf-8") == "values = [1, 2]\n"


def test_post_tool_reports_unfixable_ruff_failure(repository: Path) -> None:
    (repository / "invalid.py").write_text("if\n", encoding="utf-8")

    result = _invoke(
        repository, "post-tool", {"tool_name": "Write", "tool_input": {"file_path": "invalid.py"}}
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "invalid.py" in result.stderr


@pytest.mark.parametrize("payload", ["[]", "{broken"])
def test_invalid_hook_payload_is_reported_as_failure(repository: Path, payload: str) -> None:
    result = _invoke(repository, "pre-read", payload)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr


@pytest.mark.parametrize(
    "path",
    [
        ".venv/module.py",
        "nested/__pycache__/module.pyc",
        "htmlcov/index.html",
        "build/file",
        "dist/file",
        "data/file",
        "media/file",
        "staticfiles/file",
        ".ruff_cache/file",
    ],
)
def test_direct_reads_of_excluded_directories_are_denied(repository: Path, path: str) -> None:
    result = _invoke(
        repository, "pre-read", {"tool_name": "Read", "tool_input": {"file_path": path}}
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    assert output["permissionDecisionReason"]


@pytest.mark.parametrize(
    "path",
    [
        ".venv-verify/module.py",
        "env/module.py",
        "__pypackages__/module.py",
        "web/.next/server/page.js",
        "web/.next-approvals/server/page.js",
        "web/out/index.html",
    ],
)
def test_direct_reads_of_frontend_output_and_alternate_environments_are_denied(
    repository: Path, path: str
) -> None:
    result = _invoke(
        repository, "pre-read", {"tool_name": "Read", "tool_input": {"file_path": path}}
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    ("tool_name", "path"),
    [
        ("view_image", "web/test-results/proposal-320.png"),
        ("Read", "web/test-results/failing-test/error-context.md"),
        ("Read", "web/node_modules/next/dist/docs/index.md"),
        ("Read", "docs/.next-steps.md"),
    ],
)
def test_verification_artifacts_remain_available_for_debugging(
    repository: Path, tool_name: str, path: str
) -> None:
    result = _invoke(
        repository, "pre-read", {"tool_name": tool_name, "tool_input": {"file_path": path}}
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_mcp_multi_read_checks_absolute_and_relative_paths(repository: Path) -> None:
    result = _invoke(
        repository,
        "pre-read",
        {
            "tool_name": "mcp__filesystem__read_multiple_files",
            "tool_input": {"paths": ["src/app.py", str(repository / "data" / "fixture.json")]},
        },
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    ("tool_name", "path_key"),
    [
        ("mcp__pycharm__read_file", "file_path"),
        ("mcp__pycharm__get_file_text_by_path", "pathInProject"),
        ("mcp__pycharm__get_file_text_by_path", "filePath"),
        ("Read", "relativePath"),
    ],
)
def test_direct_read_path_aliases(repository: Path, tool_name: str, path_key: str) -> None:
    result = _invoke(
        repository,
        "pre-read",
        {"tool_name": tool_name, "tool_input": {path_key: "data/fixture.json"}},
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("project_directory", [".", "data"])
def test_ide_read_paths_use_the_explicit_project_root(
    repository: Path, project_directory: str
) -> None:
    result = _invoke(
        repository,
        "pre-read",
        {
            "tool_name": "mcp__pycharm__read_file",
            "cwd": str(repository / "data"),
            "tool_input": {
                "projectPath": str(repository / project_directory),
                "file_path": "src/app.py",
            },
        },
    )

    assert result.returncode == 0, result.stderr
    if project_directory == "data":
        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert result.stdout == ""


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("Read", {"file_path": "src/data_model.py"}),
        ("Read", {"file_path": "docs/metadata/guide.md"}),
        ("Bash", {"command": "python scripts/check.py", "path": ".venv/python.exe"}),
        ("exec_command", {"command": "cat data/fixture.json"}),
        ("Write", {"file_path": "data/fixture.json"}),
    ],
)
def test_read_guard_allows_source_and_runtime_operations(
    repository: Path, tool_name: str, arguments: dict[str, str]
) -> None:
    result = _invoke(repository, "pre-read", {"tool_name": tool_name, "tool_input": arguments})

    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


SENSITIVE_PATHS = [
    "secrets/runtime.json",
    "secrets\\database.json",
    ".env",
    ".env.production",
    ".envrc",
    "web/.env.local",
    "web\\.env.local",
    ".dev/backups/ideal-state/manifest.json",
    ".dev\\backups\\ideal-state\\dump.custom",
    "infra/terraform/terraform.tfvars",
    "infra/terraform/production.auto.tfvars.json",
    "infra/terraform/plan.tfplan",
    "infra\\terraform\\terraform.tfstate",
]


@pytest.mark.parametrize("path", SENSITIVE_PATHS)
@pytest.mark.parametrize(
    ("tool_name", "path_key"),
    [
        ("Read", "file_path"),
        ("view_image", "path"),
        ("mcp__pycharm__get_file_text_by_path", "pathInProject"),
    ],
)
def test_direct_reads_of_credentials_and_backups_are_denied_with_their_own_reason(
    repository: Path, tool_name: str, path_key: str, path: str
) -> None:
    decision = _decision(
        _invoke(repository, "pre-read", {"tool_name": tool_name, "tool_input": {path_key: path}})
    )

    assert decision is not None
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"] == (
        "Credential, secret and backup files are not read by agents."
    )


def test_environment_files_outside_the_repository_are_denied(repository: Path) -> None:
    path = repository.parent / "other-checkout" / ".env"

    decision = _decision(
        _invoke(
            repository, "pre-read", {"tool_name": "Read", "tool_input": {"file_path": str(path)}}
        )
    )

    assert decision is not None
    assert decision["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        "web/.env.example",
        "infra/terraform/terraform.tfvars.example",
        ".dev/setup.log",
        "src/pyracast/connections/secrets.py",
        "docs/secrets.md",
    ],
)
def test_example_environment_files_and_launcher_logs_remain_readable(
    repository: Path, path: str
) -> None:
    decision = _decision(
        _invoke(repository, "pre-read", {"tool_name": "Read", "tool_input": {"file_path": path}})
    )

    assert decision is None


@pytest.mark.parametrize(
    "command",
    [
        "get_file_text_by_path --pathInProject .env",
        "get_file_text_by_path --pathInProject=secrets/runtime.json",
        'get_file_text_by_path --pathInProject "web/.env.local"',
        "get_file_text_by_path --pathInProject .envrc",
        "get_file_text_by_path --pathInProject infra/terraform/terraform.tfvars.json",
        "get_file_text_by_path --pathInProject .venv/pyvenv.cfg",
        "read_file data\\fixture.json",
        "read_file '{root}/.dev/backups/x/manifest.json'",
        'read_file "{root}/data/with space.json"',
        "get_file_text_by_path --pathInProject data",
        "get_file_text_by_path --pathInProject=.venv",
        "get_file_text_by_path --pathInProject secrets",
        "get_file_text_by_path --pathInProject .venv-quality",
        "read_file data",
    ],
)
def test_ide_execute_tool_commands_naming_denied_paths_are_denied(
    repository: Path, command: str
) -> None:
    (repository / "data").mkdir()
    (repository / ".venv-quality").mkdir()
    payload: dict[str, object] = {
        "tool_name": "mcp__pycharm__execute_tool",
        "tool_input": {
            "command": command.format(root=repository.as_posix()),
            "projectPath": str(repository),
        },
    }

    decision = _decision(_invoke(repository, "pre-read", payload))

    assert decision is not None
    assert decision["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "get_file_text_by_path --pathInProject src/pyracast/settings.py",
        "search_in_files_by_text --searchText data",
        "find_files_by_name_keyword --nameKeyword secrets",
        "get_file_problems --filePath '.env.example'",
        "read_file 'unbalanced",
    ],
)
def test_ide_execute_tool_commands_without_denied_paths_are_allowed(
    repository: Path, command: str
) -> None:
    payload: dict[str, object] = {
        "tool_name": "mcp__pycharm__execute_tool",
        "tool_input": {"command": command, "projectPath": str(repository)},
    }

    assert _decision(_invoke(repository, "pre-read", payload)) is None


def test_configured_matchers_cover_reads_through_the_ide_and_shell_tools() -> None:
    hooks = json.loads(HOOK_CONFIGURATION.read_text(encoding="utf-8"))["hooks"]
    matchers = {
        group["hooks"][0]["command"].rsplit(" ", 1)[-1]: re.compile(group["matcher"])
        for group in hooks["PreToolUse"]
    }

    assert matchers["pre-read"].fullmatch("mcp__pycharm__execute_tool")
    assert matchers["pre-read"].fullmatch("Read")
    assert matchers["pre-read"].fullmatch("mcp__filesystem__read")
    assert matchers["pre-read"].fullmatch("mcp__images__view_image")
    assert not matchers["pre-read"].fullmatch("Bash")
    for tool_name in (
        "Bash",
        "PowerShell",
        "shell",
        "local_shell",
        "exec_command",
        "mcp__pycharm__execute_tool",
    ):
        assert matchers["pre-shell"].fullmatch(tool_name)
    assert not matchers["pre-shell"].fullmatch("Read")


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin main",
        "git push origin HEAD:main",
        "git -C ../pyracast push --force origin main",
        "git push origin pr/K02 main",
        "git push --all origin",
        "git push --mirror origin",
        "git status && git push origin main",
        "gh pr merge 18 --squash",
        "gh -R owner/repo pr merge 18",
        "gh workflow run release.yml --ref main",
        "gh api repos/owner/repo/actions/workflows/release.yml/dispatches -f ref=main",
        'git commit -m "Document PYRACAST_ORCHESTRATOR_PUSH=1"; git push origin main',
        "echo PYRACAST_ORCHESTRATOR_PUSH=1 && git push origin main",
        "git log --grep PYRACAST_ORCHESTRATOR_PUSH=1\ngit push origin main",
        'bash -lc "git push origin main"',
        "sh -c 'gh pr merge 18 --squash'",
        'pwsh -Command "git push origin main"',
        'powershell.exe -NoProfile -Command "gh workflow run release.yml"',
        'cmd.exe /c "git push origin main"',
        "cmd.exe /c git push origin main",
        "pwsh -Command git push origin main",
        "bash -lc 'sh -c \"git push origin main\"'",
        "git --git-dir .git push origin main",
        "git --work-tree . push origin main",
        "git --namespace release push origin main",
        "git --config-env core.editor=EDITOR push origin main",
    ],
)
@pytest.mark.parametrize(
    ("tool_name", "input_key"),
    [
        ("Bash", "command"),
        ("PowerShell", "command"),
        ("local_shell", "command"),
        ("exec_command", "cmd"),
    ],
)
def test_shell_commands_that_publish_need_the_orchestrator_marker(
    repository: Path, tool_name: str, input_key: str, command: str
) -> None:
    decision = _decision(
        _invoke(
            repository,
            "pre-shell",
            {"tool_name": tool_name, "tool_input": {input_key: command}},
            env=_environment_without_marker(),
        )
    )

    assert decision is not None
    assert decision["permissionDecision"] == "deny"
    assert "PYRACAST_ORCHESTRATOR_PUSH=1" in decision["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        "git push --force-with-lease -u origin pr/K02",
        "git push origin HEAD:refs/heads/pr/K02",
        "git push origin --delete pr/K02",
        "git commit -m 'Describe the change'",
        "git commit -m 'Explain why; git push origin main is refused'",
        "git status; git log --oneline -5",
        "gh pr create --title Title --body Body",
        "gh pr view 18 --json state",
        "echo 'git push origin main'",
        "python scripts/check.py",
        'bash -lc "git push origin pr/K02"',
        'pwsh -Command "git status"',
        'bash -lc "echo git push origin main"',
        "git --git-dir .git push origin pr/K02",
    ],
)
def test_shell_commands_that_do_not_publish_are_allowed(repository: Path, command: str) -> None:
    decision = _decision(
        _invoke(
            repository,
            "pre-shell",
            {"tool_name": "Bash", "tool_input": {"command": command}},
            env=_environment_without_marker(),
        )
    )

    assert decision is None


@pytest.mark.parametrize(
    ("tool_name", "command"),
    [
        ("Bash", r"g\it push origin main"),
        ("PowerShell", r"C:\tools\git.exe push origin main"),
        ("PowerShell", r"& 'C:\Program Files\Git\bin\git.exe' push origin main"),
    ],
)
def test_shell_executable_paths_and_escapes_need_the_orchestrator_marker(
    repository: Path, tool_name: str, command: str
) -> None:
    decision = _decision(
        _invoke(
            repository,
            "pre-shell",
            {"tool_name": tool_name, "tool_input": {"command": command}},
            env=_environment_without_marker(),
        )
    )

    assert decision is not None
    assert decision["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    ("command", "denied"),
    [
        ('execute_terminal_command --command "git push origin main"', True),
        ("execute_terminal_command --command='gh pr merge 18 --squash'", True),
        ('execute_terminal_command --command "git push origin pr/K02"', False),
        ('execute_terminal_command --command "git status"', False),
        ('get_file_text_by_path --pathInProject "git push origin main"', False),
    ],
)
def test_ide_terminal_publishing_checks_the_nested_shell_command(
    repository: Path, command: str, denied: bool
) -> None:
    decision = _decision(
        _invoke(
            repository,
            "pre-shell",
            {"tool_name": "mcp__pycharm__execute_tool", "tool_input": {"command": command}},
            env=_environment_without_marker(),
        )
    )

    if denied:
        assert decision is not None
        assert decision["permissionDecision"] == "deny"
    else:
        assert decision is None


@pytest.mark.parametrize("tool_name", ["shell", "local_shell"])
def test_shell_tools_that_pass_an_argument_vector_are_checked(
    repository: Path, tool_name: str
) -> None:
    payload: dict[str, object] = {
        "tool_name": tool_name,
        "tool_input": {"command": ["bash", "-lc", "git push"]},
    }

    decision = _decision(
        _invoke(repository, "pre-shell", payload, env=_environment_without_marker())
    )

    assert decision is not None
    assert decision["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    ("command", "environment"),
    [
        ("git push origin main", {"PYRACAST_ORCHESTRATOR_PUSH": "1"}),
        ("$env:PYRACAST_ORCHESTRATOR_PUSH='1'; git push origin main", {}),
        ("PYRACAST_ORCHESTRATOR_PUSH=1 gh pr merge 18 --squash", {}),
        ("export PYRACAST_ORCHESTRATOR_PUSH=1\ngit push origin main", {}),
    ],
)
def test_the_orchestrator_marker_allows_publishing(
    repository: Path, command: str, environment: dict[str, str]
) -> None:
    decision = _decision(
        _invoke(
            repository,
            "pre-shell",
            {"tool_name": "PowerShell", "tool_input": {"command": command}},
            env={**_environment_without_marker(), **environment},
        )
    )

    assert decision is None


def _environment_without_marker() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != "PYRACAST_ORCHESTRATOR_PUSH"}


def _fake_node(directory: Path) -> dict[str, str]:
    directory.mkdir()
    if sys.platform == "win32":
        (directory / "node.cmd").write_text(f'@"{sys.executable}" %*\r\n', encoding="utf-8")
    else:
        node = directory / "node"
        node.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        node.chmod(0o755)
    return {**os.environ, "PATH": os.pathsep.join((str(directory), os.environ["PATH"]))}


FAKE_PRETTIER = """\
import json, os, sys
from pathlib import Path
Path(os.environ["PRETTIER_LOG"]).write_text(
    json.dumps({"cwd": os.getcwd(), "args": sys.argv[1:]}), encoding="utf-8"
)
for name in sys.argv[sys.argv.index("--") + 1 :]:
    Path(name).write_text("formatted\\n", encoding="utf-8")
"""


@pytest.mark.parametrize("tool_name", ["Edit", "Write"])
def test_edit_formats_edited_web_files_with_the_local_prettier(
    repository: Path, tmp_path_factory: pytest.TempPathFactory, tool_name: str
) -> None:
    prettier = repository / "web" / "node_modules" / "prettier" / "bin" / "prettier.cjs"
    prettier.parent.mkdir(parents=True)
    prettier.write_text(FAKE_PRETTIER, encoding="utf-8")
    edited = repository / "web" / "src" / "app.tsx"
    edited.parent.mkdir(parents=True)
    edited.write_text("const  value=1\n", encoding="utf-8")
    log = tmp_path_factory.mktemp("prettier") / "log.json"
    env = {**_fake_node(tmp_path_factory.mktemp("node") / "bin"), "PRETTIER_LOG": str(log)}

    result = _invoke(
        repository,
        "post-tool",
        {"tool_name": tool_name, "tool_input": {"file_path": str(edited)}},
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert edited.read_text(encoding="utf-8") == "formatted\n"
    call = json.loads(log.read_text(encoding="utf-8"))
    assert Path(call["cwd"]).resolve() == (repository / "web").resolve()
    assert call["args"] == ["--write", "--ignore-unknown", "--", str(Path("src/app.tsx"))]


def test_web_edit_without_node_modules_is_left_alone(
    repository: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    edited = repository / "web" / "src" / "app.tsx"
    edited.parent.mkdir(parents=True)
    edited.write_text("const  value=1\n", encoding="utf-8")

    result = _invoke(
        repository,
        "post-tool",
        {"tool_name": "Write", "tool_input": {"file_path": str(edited)}},
        env=_fake_node(tmp_path_factory.mktemp("node") / "bin"),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert edited.read_text(encoding="utf-8") == "const  value=1\n"
