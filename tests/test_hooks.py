import json
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
    command: list[str], root: Path, *, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed executables, argument lists and isolated fixtures.
        command,
        cwd=root,
        input=stdin,
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
    root: Path, event: str, payload: dict[str, object] | str
) -> subprocess.CompletedProcess[str]:
    return _run(
        [sys.executable, str(root / ".agents" / "hooks.py"), event],
        root,
        stdin=payload if isinstance(payload, str) else json.dumps(payload),
    )


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
