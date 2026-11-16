import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import agent_graph


@pytest.mark.parametrize("windows", [False, True])
def test_graph_cache_uses_a_user_directory_and_isolates_checkouts(
    tmp_path: Path, windows: bool
) -> None:
    variable = "LOCALAPPDATA" if windows else "XDG_CACHE_HOME"
    environment = {variable: str(tmp_path / "user cache")}
    first = agent_graph.graph_directory(tmp_path / "one" / "project", environment, windows=windows)
    second = agent_graph.graph_directory(tmp_path / "two" / "project", environment, windows=windows)
    assert first.is_relative_to(tmp_path / "user cache" / "code-review-graph")
    assert first != second
    assert not first.exists()


def test_graph_cache_respects_an_external_override(tmp_path: Path) -> None:
    external = tmp_path / "external cache"
    assert (
        agent_graph.graph_directory(
            tmp_path / "project", {"CRG_HOME": str(external)}, windows=False
        )
        == external
    )


@pytest.mark.parametrize("override", ["relative-cache", ".", "nested/cache"])
def test_relative_graph_cache_is_rejected(tmp_path: Path, override: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        agent_graph.graph_directory(tmp_path, {"CRG_HOME": override}, windows=False)


def test_graph_cache_cannot_write_into_source_tree(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        agent_graph.graph_directory(tmp_path, {"CRG_HOME": str(tmp_path / "cache")}, windows=False)


def test_graph_cache_resolves_symlinks_before_validating(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    link = tmp_path / "external-link"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("Creating a directory symlink needs Windows Developer Mode or permission.")
    with pytest.raises(ValueError, match="including symlinks"):
        agent_graph.graph_directory(root, {"CRG_HOME": str(link)}, windows=False)


@pytest.mark.parametrize("operation", ["build", "update", "query"])
def test_graph_commands_share_the_same_cache_without_mutating_session_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str
) -> None:
    external = tmp_path / "external cache"
    monkeypatch.setenv("CRG_HOME", str(external))
    monkeypatch.setattr(shutil, "which", lambda _: "/installed/code-review-graph")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(subprocess, "run", run)
    arguments = (
        [operation, "callers_of", "function with spaces"] if operation == "query" else [operation]
    )
    assert agent_graph.main(arguments) == 7
    command, options = calls[0]
    assert options["env"]["CRG_HOME"] == str(external)
    assert options["env"]["CRG_DATA_DIR"] == str(external)
    assert os.environ["CRG_HOME"] == str(external)
    assert options["check"] is False
    assert "shell" not in options
    assert command[0] == "/installed/code-review-graph"
    assert command[2:4] == ["--repo", str(Path(agent_graph.__file__).resolve().parents[1])]
    if operation == "query":
        assert command[4:] == ["--", "callers_of", "function with spaces"]
        assert "--data-dir" not in command
    else:
        assert command[4:] == ["--skip-flows", "--data-dir", str(external)]


def test_missing_graph_cli_is_actionable_and_does_not_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "external"))
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert agent_graph.main(["update"]) == 1
    assert "user-level code-review-graph CLI" in capsys.readouterr().err


@pytest.mark.parametrize("operation", ["install", "watch", "daemon", "embed", "wiki"])
def test_graph_wrapper_only_exposes_local_build_update_and_query(operation: str) -> None:
    with pytest.raises(SystemExit) as error:
        agent_graph.main([operation])
    assert error.value.code == 2


def test_default_graph_cache_only_enters_the_child_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CRG_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user cache"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "user cache"))
    monkeypatch.setattr(shutil, "which", lambda _: "/installed/code-review-graph")
    child_environments: list[dict[str, str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        child_environments.append(kwargs["env"])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert agent_graph.main(["query", "tests_for", "sample"]) == 0
    assert Path(child_environments[0]["CRG_HOME"]).is_relative_to(tmp_path / "user cache")
    assert "CRG_HOME" not in os.environ


@pytest.mark.skipif(
    shutil.which("code-review-graph") is None, reason="User-level graph CLI is absent."
)
def test_first_query_never_creates_a_graph_inside_the_checkout(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(agent_graph.__file__, scripts / "agent_graph.py")
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)  # noqa: S603, S607 -- fixed git command.
    environment = {**os.environ, "CRG_HOME": str(tmp_path / "external cache")}
    environment.pop("CRG_DATA_DIR", None)
    result = subprocess.run(
        [sys.executable, "scripts/agent_graph.py", "query", "callers_of", "--", "--help"],
        cwd=repository,
        env=environment,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 1
    assert not (repository / ".code-review-graph").exists()
    assert b"No graph found" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "target",
    [
        "--help",
        "--",
        "--repo=/different/repository",
        "--data-dir=/different/cache",
        "--embedding-provider=openai",
        "module::function with spaces; & $(command)",
        "module::ölçüm",
    ],
)
def test_query_targets_cannot_become_child_cli_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str
) -> None:
    external = tmp_path / "external cache"
    root = Path(agent_graph.__file__).resolve().parents[1]
    monkeypatch.setenv("CRG_HOME", str(external))
    monkeypatch.setattr(shutil, "which", lambda _: "/installed/code-review-graph")

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        parser = argparse.ArgumentParser()
        parser.add_argument("--repo")
        parser.add_argument("pattern", choices=("callers_of",))
        parser.add_argument("target")
        parsed = parser.parse_args(command[2:])
        assert parsed.repo == str(root)
        assert parsed.pattern == "callers_of"
        assert parsed.target == target
        assert kwargs["env"]["CRG_HOME"] == str(external)
        assert kwargs["env"]["CRG_DATA_DIR"] == str(external)
        assert "shell" not in kwargs
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert agent_graph.main(["query", "callers_of", "--", target]) == 0


@pytest.mark.parametrize("suffix", [".CMD", ".bat"])
def test_windows_batch_launchers_are_rejected_before_starting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    suffix: str,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "external cache"))
    monkeypatch.setattr(shutil, "which", lambda _: f"/installed/code-review-graph{suffix}")

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        pytest.fail("The Windows batch launcher must not start.")

    monkeypatch.setattr(subprocess, "run", run)
    assert agent_graph.main(["query", "callers_of", "--", 'symbol" & command & "']) == 1
    assert "native code-review-graph executable" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("platform", "executable"),
    [
        ("win32", "/installed/code-review-graph.EXE"),
        ("linux", "/installed/code-review-graph"),
        ("linux", "/installed/code-review-graph.sh"),
    ],
)
def test_native_and_unix_entry_points_remain_supported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, executable: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "external cache"))
    monkeypatch.setattr(shutil, "which", lambda _: executable)
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert agent_graph.main(["query", "callers_of", "symbol"]) == 0
    assert calls[0][0] == executable
