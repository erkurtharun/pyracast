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
    # CI may be unable to autoload Microsoft.PowerShell.Security; use native .NET ACL APIs.
    probe = subprocess.run(  # noqa: S603 -- fixed native ACL inspection; path arrives via stdin.
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop'; "
            "$paths=ConvertFrom-Json ([Console]::In.ReadToEnd()); "
            "$PSModuleAutoLoadingPreference='None'; "
            "$entries=@(foreach ($p in $paths) { "
            "$acl=if ([IO.Directory]::Exists($p)) { [IO.Directory]::GetAccessControl($p) } "
            "else { [IO.File]::GetAccessControl($p) }; "
            "$rules=$acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]); "
            "[pscustomobject]@{ Path=$p; "
            "Owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value; "
            "FileSystem=[IO.DriveInfo]::new([IO.Path]::GetPathRoot($p)).DriveFormat; "
            "Sddl=$acl.GetSecurityDescriptorSddlForm("
            "[Security.AccessControl.AccessControlSections]::All); "
            "Rules=@($rules | ForEach-Object { [pscustomobject]@{ "
            "Identity=$_.IdentityReference.Value; Access=[int]$_.AccessControlType; "
            "Rights=[int]$_.FileSystemRights; Propagation=[int]$_.PropagationFlags; "
            "Inherited=[bool]$_.IsInherited } }) } }); "
            "[pscustomobject]@{ "
            "CurrentUser=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
            "Entries=$entries } | ConvertTo-Json -Compress -Depth 5",
        ],
        input=json.dumps([str(root), str(child)]),
        text=True,
        capture_output=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    diagnostic = json.loads(probe.stdout)
    # Windows may split an inherited ACE into effective-only and inherit-only entries.
    assert any(
        rule["Identity"] == diagnostic["CurrentUser"]
        and rule["Access"] == 0
        and rule["Rights"] & 2032127 == 2032127
        and not rule["Propagation"] & 2
        and rule["Inherited"]
        for rule in diagnostic["Entries"][1]["Rules"]
    ), json.dumps(diagnostic, indent=2)
