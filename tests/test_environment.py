import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from pyracast.environment import load_environment


def test_local_environment_loads_literal_values_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    expected = "test-${literal}-credential"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYRACAST_EMAIL_PASSWORD", raising=False)
    (tmp_path / ".env").write_text(f"PYRACAST_EMAIL_PASSWORD='{expected}'\n", encoding="utf-8")

    load_environment()

    assert os.environ["PYRACAST_EMAIL_PASSWORD"] == expected
    assert capsys.readouterr() == ("", "")
    assert caplog.text == ""


def test_local_environment_preserves_existing_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = "process-credential"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYRACAST_EMAIL_PASSWORD", expected)
    (tmp_path / ".env").write_text("PYRACAST_EMAIL_PASSWORD=file-credential\n", encoding="utf-8")

    load_environment()

    assert os.environ["PYRACAST_EMAIL_PASSWORD"] == expected


def test_missing_local_environment_does_not_search_parent_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    monkeypatch.delenv("PYRACAST_EMAIL_PASSWORD", raising=False)
    (tmp_path / ".env").write_text("PYRACAST_EMAIL_PASSWORD=parent-credential\n", encoding="utf-8")

    load_environment()

    assert "PYRACAST_EMAIL_PASSWORD" not in os.environ


def test_settings_load_local_environment_before_reading_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = "local-test-key"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYRACAST_SECRET_KEY", raising=False)
    monkeypatch.setenv("PYRACAST_EMAIL_BACKEND", "smtp")
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


def test_local_console_mail_prints_messages_without_smtp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYRACAST_DEBUG", "1")
    monkeypatch.setenv("PYRACAST_EMAIL_BACKEND", "console")
    configuration = runpy.run_path(str(Path(__file__).parents[1] / "src/pyracast/settings.py"))
    assert configuration["MAILERS"]["default"]["BACKEND"] == (
        "django.core.mail.backends.console.EmailBackend"
    )
    with override_settings(MAILERS=configuration["MAILERS"]):
        assert (
            mail.send_mail(
                "Local verification",
                "Development code: TEST-CODE",
                "local@localhost",
                ["dev@localhost"],
            )
            == 1
        )
    output = capsys.readouterr()
    assert "Development code: TEST-CODE" in output.out
    assert output.err == ""


@pytest.mark.parametrize(("debug", "backend"), [("0", "console"), ("1", "invalid")])
def test_unsafe_mail_backend_configuration_stops_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debug: str, backend: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYRACAST_DEBUG", debug)
    monkeypatch.setenv("PYRACAST_EMAIL_BACKEND", backend)
    with pytest.raises(ImproperlyConfigured, match="PYRACAST_EMAIL_BACKEND"):
        runpy.run_path(str(Path(__file__).parents[1] / "src/pyracast/settings.py"))


@pytest.mark.parametrize("debug", ["0", "1"])
def test_smtp_remains_the_default_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debug: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYRACAST_DEBUG", debug)
    monkeypatch.delenv("PYRACAST_EMAIL_BACKEND", raising=False)
    configuration = runpy.run_path(str(Path(__file__).parents[1] / "src/pyracast/settings.py"))
    assert configuration["MAILERS"]["default"]["BACKEND"] == (
        "django.core.mail.backends.smtp.EmailBackend"
    )
