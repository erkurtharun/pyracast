import os
import subprocess
import sys
from pathlib import Path

import pyracast


def test_installed_package_imports_outside_repository(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import pyracast; print(pyracast.__version__)",
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == pyracast.__version__
