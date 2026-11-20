import ast
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "helper",
    [
        "backup.py",
        "bootstrap_database.py",
        "edge_gate.py",
        "host.py",
        "host_contract.py",
        "image_rescan.py",
        "operations_log.py",
        "pin_release_images.py",
        "rehearsal.py",
        "release_record.py",
        "release_scope.py",
        "release_state.py",
        "reset_database.py",
        "runtime_secrets.py",
        "watchdog.py",
    ],
)
def test_host_helpers_accept_ubuntu_system_python_312_syntax(helper: str) -> None:
    source = Path(__file__).resolve().parents[1] / "deploy" / helper
    ast.parse(source.read_text(), filename=str(source), feature_version=(3, 12))


@pytest.mark.parametrize(
    "helper",
    ["bootstrap_database.py", "backup.py", "rehearsal.py", "release_state.py", "reset_database.py"],
)
def test_smoke_helpers_run_without_application_dependencies_or_project_working_directory(
    tmp_path: Path,
    helper: str,
) -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(  # noqa: S603 -- The interpreter and helper path are repository controlled.
        [sys.executable, "-I", "-S", str(root / "deploy" / helper), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "usage:" in result.stdout
    assert result.stderr == ""


def test_the_installers_recovery_reader_runs_as_a_host_script(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "host.json"
    config.write_text('{"backup_bucket": "pyracast-backups", "runtime_secret_version": "1"}')
    result = subprocess.run(  # noqa: S603 -- The interpreter and helper path are repository controlled.
        [
            sys.executable,
            "-I",
            "-S",
            str(root / "deploy/release_record.py"),
            str(config),
            "recovery",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "in this maintenance" in result.stdout
    assert result.stderr == ""
