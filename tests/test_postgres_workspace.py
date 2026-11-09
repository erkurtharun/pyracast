import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from scripts.start_postgres import windows_workspace


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows filesystem ACLs")
def test_windows_workspace_grants_inherited_access_to_current_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    root = windows_workspace()
    child = root / "pgsql"
    child.mkdir()
    powershell = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    probe = subprocess.run(  # noqa: S603 -- fixed native ACL inspection; path arrives via stdin.
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$p=[Console]::In.ReadLine(); "
            "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User; "
            "$rules=(Get-Acl -LiteralPath $p).GetAccessRules("
            "$true,$true,[Security.Principal.SecurityIdentifier]); "
            "ConvertTo-Json -Compress -InputObject @($rules | Where-Object { "
            "$_.IdentityReference -eq $sid -and "
            "$_.AccessControlType -eq 'Allow' } | Select-Object "
            "FileSystemRights,InheritanceFlags,IsInherited)",
        ],
        input=str(child),
        text=True,
        capture_output=True,
        check=True,
    )
    rules = json.loads(probe.stdout)
    assert any(
        rule["FileSystemRights"] == 2032127
        and rule["InheritanceFlags"] == 3
        and rule["IsInherited"]
        for rule in rules
    )
