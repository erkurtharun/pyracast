import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from pyracast.environment import load_environment


def test_local_environment_loads_literal_values_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SOLCAST_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "SOLCAST_API_KEY='test-${literal}-credential'\n", encoding="utf-8"
    )

    load_environment()

    assert os.environ["SOLCAST_API_KEY"] == "test-${literal}-credential"
    assert capsys.readouterr() == ("", "")
    assert caplog.text == ""


def test_local_environment_preserves_existing_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SOLCAST_API_KEY", "process-credential")
    (tmp_path / ".env").write_text("SOLCAST_API_KEY=file-credential\n", encoding="utf-8")

    load_environment()

    assert os.environ["SOLCAST_API_KEY"] == "process-credential"


def test_missing_local_environment_does_not_search_parent_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    monkeypatch.delenv("SOLCAST_API_KEY", raising=False)
    (tmp_path / ".env").write_text("SOLCAST_API_KEY=parent-credential\n", encoding="utf-8")

    load_environment()

    assert "SOLCAST_API_KEY" not in os.environ


def test_settings_load_local_environment_before_reading_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = "local-test-key"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYRACAST_SECRET_KEY", raising=False)
    (tmp_path / ".env").write_text("PYRACAST_SECRET_KEY=local-test-key\n", encoding="utf-8")

    configuration = runpy.run_path(str(Path(__file__).parents[1] / "src/pyracast/settings.py"))

    assert configuration["SECRET_KEY"] == expected


@pytest.mark.parametrize("level", ["INFO", "WARNING"])
def test_application_logs_use_stdout_and_configured_level(tmp_path: Path, level: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import logging.config; "
            "from pyracast.settings import LOGGING; "
            "logging.config.dictConfig(LOGGING); "
            "logger = logging.getLogger('pyracast.probe'); "
            "logger.info('info-event'); logger.warning('warning-event')",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYRACAST_LOG_LEVEL": level},
        capture_output=True,
        text=True,
        check=True,
    )

    assert "warning-event" in result.stdout
    assert ("info-event" in result.stdout) == (level == "INFO")
    assert result.stderr == ""
