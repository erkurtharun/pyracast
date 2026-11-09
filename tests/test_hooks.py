import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOK_SOURCE = Path(__file__).resolve().parents[1] / ".codex" / "hooks" / "quality.py"


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


def _git(root: Path, *arguments: str) -> None:
    result = _run(["git", *arguments], root)
    assert result.returncode == 0, result.stderr


def _commit(root: Path) -> None:
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Hook Test",
        "-c",
        "user.email=hook@example.test",
        "commit",
        "-qm",
        "Fixture",
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    hooks = tmp_path / ".codex" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copyfile(HOOK_SOURCE, hooks / "quality.py")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "check.py").write_text(
        "from pathlib import Path\n"
        "with Path('check-runs.txt').open('a', encoding='utf-8') as marker:\n"
        "    marker.write('checked\\n')\n"
        "print('Checker completed successfully.')\n",
        encoding="utf-8",
    )
    _git(tmp_path, "init", "-q")
    _commit(tmp_path)
    return tmp_path


def _invoke(
    root: Path, event: str, payload: dict[str, object] | str
) -> subprocess.CompletedProcess[str]:
    return _run(
        [sys.executable, str(root / ".codex" / "hooks" / "quality.py"), event],
        root,
        stdin=payload if isinstance(payload, str) else json.dumps(payload),
    )


@pytest.mark.parametrize("tool_name", ["Edit", "Write", "MultiEdit"])
def test_edit_formats_and_fixes_only_the_edited_python_file(
    repository: Path, tool_name: str
) -> None:
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
    assert edited.read_text(encoding="utf-8").lstrip("\n") == "values = [1, 2]\n"
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


@pytest.mark.parametrize("change", ["untracked", "modified", "deleted", "renamed"])
def test_stop_checks_dirty_python_with_real_git_status(repository: Path, change: str) -> None:
    original = repository / "solar ölçüm.py"
    original.write_text("value = 1\n", encoding="utf-8")
    if change != "untracked":
        _commit(repository)
    if change == "modified":
        original.write_text("value = 2\n", encoding="utf-8")
    elif change == "deleted":
        original.unlink()
    elif change == "renamed":
        _git(repository, "mv", original.name, "renamed θ.txt")

    result = _invoke(repository, "stop", {"stop_hook_active": False})

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {}
    assert result.stderr == ""
    assert (repository / "check-runs.txt").read_text(encoding="utf-8") == "checked\n"


def test_stop_does_not_misread_a_rename_source_as_a_status_record(repository: Path) -> None:
    original = repository / "original file.txt"
    original.write_text("source\n", encoding="utf-8")
    _commit(repository)
    _git(repository, "mv", original.name, "renamed file.txt")
    (repository / "untracked nested").mkdir()
    (repository / "untracked nested" / "ölçüm.py").write_text("value = 1\n", encoding="utf-8")

    result = _invoke(repository, "stop", {})

    assert result.returncode == 0, result.stderr
    assert (repository / "check-runs.txt").read_text(encoding="utf-8") == "checked\n"


@pytest.mark.parametrize("active", [False, True])
def test_stop_skips_clean_python_and_recursive_stop(repository: Path, active: bool) -> None:
    (repository / "notes.txt").write_text("changed\n", encoding="utf-8")
    if active:
        (repository / "untracked.py").write_text("value = 1\n", encoding="utf-8")

    result = _invoke(repository, "stop", {"stop_hook_active": active})

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {}
    assert not (repository / "check-runs.txt").exists()


def test_stop_returns_last_forty_failure_lines(repository: Path) -> None:
    (repository / "scripts" / "check.py").write_text(
        "import sys\nfor index in range(65):\n    print(f'failure {index}')\nsys.exit(1)\n",
        encoding="utf-8",
    )

    result = _invoke(repository, "stop", {})

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.splitlines() == [f"failure {index}" for index in range(25, 65)]


@pytest.mark.parametrize("payload", ["[]", "{broken"])
def test_invalid_hook_payload_is_reported_as_failure(repository: Path, payload: str) -> None:
    result = _invoke(repository, "stop", payload)

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
