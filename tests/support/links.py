import importlib
import os
from pathlib import Path


def link(target: Path, at: Path) -> None:
    try:
        at.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        importlib.import_module("_winapi").CreateJunction(str(target), str(at))


def unlink(at: Path) -> None:
    if at.is_junction():
        os.rmdir(at)
    else:
        at.unlink()
