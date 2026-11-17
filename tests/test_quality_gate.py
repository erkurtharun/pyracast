import os
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
