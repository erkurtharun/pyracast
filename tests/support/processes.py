import os
import sys
from pathlib import Path


def interpreter() -> str:
    # A Windows virtual environment's python.exe runs the base interpreter as a child that outlives
    # a kill of the launcher for a moment and keeps the launcher's files open.
    base = Path(sys.base_prefix) / "python.exe"
    return str(base) if os.name == "nt" and base.is_file() else sys.executable
