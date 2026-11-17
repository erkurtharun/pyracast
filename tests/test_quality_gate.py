import json
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import psutil
import pytest

from scripts import check


def record_commands(
    monkeypatch: pytest.MonkeyPatch, physical_cores: int | None, logical_cores: int
) -> list[dict[str, str]]:
    environments: list[dict[str, str]] = []

    def record(
        command: Sequence[str], *, env: dict[str, str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        environments.append(env)
        return subprocess.CompletedProcess(command, 0)

    def cpu_count(logical: bool = True) -> int | None:
        return logical_cores if logical else physical_cores

    monkeypatch.setattr(subprocess, "run", record)
    monkeypatch.setattr(psutil, "cpu_count", cpu_count)
    monkeypatch.setattr(os, "cpu_count", lambda: logical_cores)
    monkeypatch.setattr(check, "finish_shard", lambda output: None)
    return environments


@pytest.mark.parametrize("shard", [False, True], ids=["full", "shard"])
@pytest.mark.parametrize(
    ("physical_cores", "logical_cores", "explicit", "expected"),
    [
        (12, 16, None, "6"),
        (4, 8, None, "4"),
        (None, 2, None, "2"),
        (12, 16, "3", "3"),
    ],
)
def test_gate_caps_unset_xdist_workers_and_keeps_an_explicit_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    physical_cores: int | None,
    logical_cores: int,
    explicit: str | None,
    expected: str,
    shard: bool,
) -> None:
    environments = record_commands(monkeypatch, physical_cores, logical_cores)
    if explicit is None:
        monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    else:
        monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", explicit)
    argv = ["--shard", "1/2", "--shard-output", str(tmp_path / "shard")] if shard else []

    assert check.main(argv) == 0

    assert environments
    assert {env.get("PYTEST_XDIST_AUTO_NUM_WORKERS") for env in environments} == {expected}
    assert f"pytest-xdist workers: {expected}" in capsys.readouterr().out


@pytest.mark.parametrize("explicit", ["auto", "four", "2.5"])
def test_gate_rejects_a_worker_count_that_xdist_would_ignore(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], explicit: str
) -> None:
    environments = record_commands(monkeypatch, 12, 16)
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", explicit)

    assert check.main([]) == 2

    assert environments == []
    assert f"PYTEST_XDIST_AUTO_NUM_WORKERS must be an integer, got {explicit!r}" in (
        capsys.readouterr().err
    )


def record_runs(monkeypatch: pytest.MonkeyPatch, npm: str | None) -> list[list[str]]:
    commands: list[list[str]] = []

    def record(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", record)
    monkeypatch.setattr(check, "web_npm", lambda root: npm)
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "2")
    return commands


def django_command(command: list[str]) -> str | None:
    if command[1:3] != ["-m", "django"]:
        return None
    return " ".join(part for part in command[3:] if part != "--settings=pyracast.settings")


def test_gate_runs_every_static_ci_command(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    prefix = "uv run --locked --no-sync --no-build "
    ci_django = {
        line.split(prefix + "pyracast ", 1)[1].strip()
        for line in workflow.splitlines()
        if prefix + "pyracast " in line
    } - {"migrate --noinput", "migrate --check"}
    ci_scripts = {
        line.split(prefix + "python ", 1)[1].strip()
        for line in workflow.splitlines()
        if prefix + "python scripts/" in line and line.rstrip().endswith("--check")
    }
    package = json.loads((root / "web/package.json").read_text(encoding="utf-8"))
    commands = record_runs(monkeypatch, "npm")

    assert check.main([]) == 0

    assert ci_django == {"check"}
    assert ci_django <= {django_command(command) for command in commands}
    assert ci_scripts == {"scripts/generate_api_schema.py --check"}
    assert ci_scripts <= {" ".join(command[1:]) for command in commands}
    assert ["npm", "--prefix", "web", "run", "check:static"] in commands
    for script in ("check", "check:coverage"):
        assert package["scripts"][script].startswith("npm run check:static && ")
    assert commands[-1][1:3] == ["-m", "pytest"]


def test_web_stages_report_an_explicit_skip_without_node_modules(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands = record_runs(monkeypatch, None)

    assert check.main([]) == 0

    assert not any("generate_api_schema.py" in " ".join(command) for command in commands)
    assert not any("check:static" in command for command in commands)
    output = capsys.readouterr().out
    assert output.count("SKIPPED web:") == 1
    assert "npm ci" in output


def test_static_only_runs_every_check_except_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = record_runs(monkeypatch, "npm")

    assert check.main(["--static-only"]) == 0

    assert commands
    assert not any(command[1:3] == ["-m", "pytest"] for command in commands)
    assert ["npm", "--prefix", "web", "run", "check:static"] in commands


def test_tests_only_runs_only_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = record_runs(monkeypatch, "npm")

    assert check.main(["--tests-only"]) == 0

    assert len(commands) == 1
    assert commands[0][1:3] == ["-m", "pytest"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--", "-k", "shard and not real", "tests/test_quality_gate.py"],
        ["--tests-only", "--", "-k", "shard and not real", "tests/test_quality_gate.py"],
    ],
    ids=["full", "tests-only"],
)
def test_arguments_after_a_double_dash_reach_pytest(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    commands = record_runs(monkeypatch, None)

    assert check.main(argv) == 0

    assert commands[-1][1:3] == ["-m", "pytest"]
    assert commands[-1][-3:] == ["-k", "shard and not real", "tests/test_quality_gate.py"]
    assert all("tests/test_quality_gate.py" not in command for command in commands[:-1])


def test_shard_runs_receive_pytest_arguments_after_a_double_dash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = record_runs(monkeypatch, None)
    monkeypatch.setattr(check, "finish_shard", lambda output: None)

    argv = ["--shard", "1/2", "--shard-output", str(tmp_path / "shard"), "--", "-x"]
    assert check.main(argv) == 0

    assert commands[-1][-1] == "-x"
    assert "scripts.pytest_shard" in commands[-1]


@pytest.mark.parametrize(
    "argv",
    [
        ["--static-only", "--tests-only"],
        ["--static-only", "--", "-x"],
        ["--static-only", "--shard", "1/2", "--shard-output", "out"],
        ["--tests-only", "--combine-shards", "one"],
        ["--static-only", "--combine-shards", "one"],
        ["--combine-shards", "one", "--", "-x"],
    ],
)
def test_gate_rejects_contradictory_modes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    commands = record_runs(monkeypatch, "npm")

    with pytest.raises(SystemExit) as raised:
        check.main(argv)

    assert raised.value.code == 2
    assert commands == []
    assert "error:" in capsys.readouterr().err


def test_gate_reports_the_duration_of_every_stage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands = record_runs(monkeypatch, "npm")

    assert check.main([]) == 0

    timings = re.findall(r"^Finished (.+) in \d+\.\ds$", capsys.readouterr().out, re.MULTILINE)
    assert len(timings) == len(commands)
    assert len(set(timings)) == len(timings)
    assert {"django check", "API schema", "web static checks", "pytest"} <= set(timings)


def test_gate_stops_at_the_first_failing_stage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands: list[list[str]] = []

    def fail_mypy(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 3 if "mypy" in command else 0)

    monkeypatch.setattr(subprocess, "run", fail_mypy)
    monkeypatch.setattr(check, "web_npm", lambda root: None)

    assert check.main(["--static-only"]) == 3

    assert commands[-1][1:] == ["-m", "mypy"]
    assert re.search(r"^Failed mypy in \d+\.\ds \(exit 3\)$", capsys.readouterr().out, re.M)
