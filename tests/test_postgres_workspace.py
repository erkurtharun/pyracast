import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from scripts.start_postgres import windows_workspace


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows filesystem ACLs")
@pytest.mark.parametrize(
    ("relative_path", "is_directory"),
    [("pgsql", True), ("pgsql/share", True), ("pgsql/share/postgres.bki", False)],
)
def test_windows_workspace_grants_inherited_access_to_current_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative_path: str, is_directory: bool
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    root = windows_workspace()
    child = root / relative_path
    child.parent.mkdir(parents=True, exist_ok=True)
    if is_directory:
        child.mkdir()
    else:
        child.touch()
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
            "$_.AccessControlType -eq 'Allow' } | ForEach-Object { [pscustomobject]@{ "
            "Rights=[int]$_.FileSystemRights; Propagation=[int]$_.PropagationFlags; "
            "Inherited=[bool]$_.IsInherited } })",
        ],
        input=str(child),
        text=True,
        capture_output=True,
        check=True,
    )
    rules = json.loads(probe.stdout)
    # Windows may split an inherited ACE into effective-only and inherit-only entries.
    assert any(
        rule["Rights"] & 2032127 == 2032127 and not rule["Propagation"] & 2 and rule["Inherited"]
        for rule in rules
    ), rules
