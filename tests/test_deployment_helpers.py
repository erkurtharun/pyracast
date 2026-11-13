import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("helper", ["bootstrap_database.py", "backup.py"])
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
