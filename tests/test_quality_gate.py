import os
import subprocess
from collections.abc import Sequence

import psutil
import pytest

from scripts import check


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
    physical_cores: int | None,
    logical_cores: int,
    explicit: str | None,
    expected: str,
) -> None:
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
    if explicit is None:
        monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    else:
        monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", explicit)

    assert check.main([]) == 0

    assert environments
    assert {env.get("PYTEST_XDIST_AUTO_NUM_WORKERS") for env in environments} == {expected}
    assert f"pytest-xdist workers: {expected}" in capsys.readouterr().out
