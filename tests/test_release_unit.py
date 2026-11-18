import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from deploy import release_unit

SHA = "c" * 40
STAGE = f"/run/pyracast-release-{SHA}-123-2"
IMAGES = [
    "us-docker.pkg.dev/project/repo/backend@sha256:" + "0" * 64,
    "us-docker.pkg.dev/project/repo/web@sha256:" + "1" * 64,
    "us-docker.pkg.dev/project/repo/gateway@sha256:" + "2" * 64,
    "us-docker.pkg.dev/project/repo/postgres@sha256:" + "3" * 64,
]


class Host:
    def __init__(self, stage: Path) -> None:
        self.stage = stage
        self.commands: list[list[str]] = []
        self.active: list[bool] = []
        self.exit_status: str | None = None
        self.installer_status = 0
        self.sleeps: list[float] = []

    def run(self, command: list[str], **options: Any) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(command)
        if command[0] == "systemctl":
            active = self.active.pop(0) if self.active else False
            if not active and self.exit_status is not None:
                (self.stage / release_unit.STATUS).write_text(self.exit_status)
            return subprocess.CompletedProcess(command, 0 if active else 3)
        if command[0] == "bash":
            return subprocess.CompletedProcess(command, self.installer_status)
        return subprocess.CompletedProcess(command, 0)

    def journal_reads(self) -> list[list[str]]:
        return [command for command in self.commands if command[0] == "journalctl"]

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    stage = tmp_path / f"pyracast-release-{SHA}-123-2"
    stage.mkdir()
    fake = Host(stage)
    monkeypatch.setattr(subprocess, "run", fake.run)
    monkeypatch.setattr(release_unit.time, "sleep", fake.sleep)
    return fake


@pytest.mark.parametrize(
    "value",
    [
        STAGE,
        f"/run/pyracast-release-{SHA}-1-1",
    ],
)
def test_a_release_stage_names_its_commit_run_and_attempt(value: str) -> None:
    assert release_unit.stage_directory(value) == Path(value)


@pytest.mark.parametrize(
    "value",
    [
        "/run/pyracast-release",
        f"/run/pyracast-release-{SHA}-123",
        f"/run/pyracast-release-{SHA.upper()}-1-1",
        f"/srv/pyracast-release-{SHA}-1-1",
        f"/run/pyracast-release-{SHA}-1-1/../x",
        f"/run/pyracast-release-{SHA}-1-1 ",
    ],
)
def test_any_other_stage_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="release stage"):
        release_unit.stage_directory(value)


def test_the_unit_is_named_after_its_stage() -> None:
    assert release_unit.unit_name(Path(STAGE)) == f"pyracast-release-{SHA}-123-2.service"


def test_start_runs_the_installer_in_a_transient_unit_and_follows_it(host: Host) -> None:
    host.exit_status = "0\n"

    assert release_unit.start(host.stage, [SHA, *IMAGES]) == 0

    unit = release_unit.unit_name(host.stage)
    started, *followed = host.commands
    assert started == [
        "systemd-run",
        f"--unit={unit}",
        "--service-type=exec",
        f"--description=Pyracast release {SHA}",
        "--quiet",
        "--",
        sys.executable,
        str(host.stage / "bundle" / "deploy" / "release_unit.py"),
        "run",
        str(host.stage),
        SHA,
        *IMAGES,
    ]
    assert followed[0] == ["systemctl", "is-active", "--quiet", unit]
    assert followed[1] == [
        "journalctl",
        f"--unit={unit}",
        f"--cursor-file={host.stage / release_unit.CURSOR}",
        "--output=cat",
        "--no-pager",
        "--quiet",
    ]


def test_run_executes_the_bundled_installer_and_records_its_status(host: Host) -> None:
    host.installer_status = 45

    assert release_unit.run(host.stage, [SHA, *IMAGES]) == 45

    (command,) = host.commands
    assert command == [
        "bash",
        str(host.stage / "bundle" / "deploy" / "install-release.sh"),
        SHA,
        *IMAGES,
        str(host.stage / "bundle"),
        str(host.stage / "migration.json"),
    ]
    assert (host.stage / release_unit.STATUS).read_text() == "45\n"
    assert [path.name for path in host.stage.iterdir()] == [release_unit.STATUS]


def test_run_records_a_signal_as_the_shell_reports_it(host: Host) -> None:
    host.installer_status = -9

    assert release_unit.run(host.stage, [SHA, *IMAGES]) == 137
    assert (host.stage / release_unit.STATUS).read_text() == "137\n"


def test_follow_streams_the_journal_until_the_installer_status_appears(host: Host) -> None:
    host.active = [True, True, False]
    host.exit_status = "43\n"

    assert release_unit.follow(host.stage) == 43
    assert len(host.journal_reads()) == 3
    assert host.sleeps == [release_unit.POLL_SECONDS, release_unit.POLL_SECONDS]


def test_follow_never_returns_the_status_the_workflow_reads_as_a_lost_connection(
    host: Host,
) -> None:
    host.exit_status = "255\n"

    assert release_unit.follow(host.stage) == 1


def test_follow_reports_a_unit_that_ended_without_a_status(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.active = [True, False]

    assert release_unit.follow(host.stage) == 1
    assert "recorded no status" in capsys.readouterr().err
    assert host.sleeps == [release_unit.POLL_SECONDS]


@pytest.mark.parametrize("content", ["", "x\n", "-1\n", "256\n"])
def test_follow_refuses_a_status_file_it_cannot_read(host: Host, content: str) -> None:
    (host.stage / release_unit.STATUS).write_text(content)

    with pytest.raises(ValueError, match="status"):
        release_unit.follow(host.stage)


def test_main_dispatches_each_subcommand(
    host: Host, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, Path, list[str]]] = []
    monkeypatch.setattr(release_unit, "stage_directory", lambda value: Path(value))
    monkeypatch.setattr(
        release_unit, "start", lambda stage, values: calls.append(("start", stage, values)) or 3
    )
    monkeypatch.setattr(
        release_unit, "run", lambda stage, values: calls.append(("run", stage, values)) or 4
    )
    monkeypatch.setattr(
        release_unit, "follow", lambda stage: calls.append(("follow", stage, [])) or 5
    )
    stage = str(host.stage)

    assert release_unit.main(["start", stage, SHA, *IMAGES]) == 3
    assert release_unit.main(["run", stage, SHA, *IMAGES]) == 4
    assert release_unit.main(["follow", stage]) == 5
    assert calls == [
        ("start", host.stage, [SHA, *IMAGES]),
        ("run", host.stage, [SHA, *IMAGES]),
        ("follow", host.stage, []),
    ]


def test_main_refuses_an_invalid_stage(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        release_unit.main(["follow", "/srv/elsewhere"])
    assert refused.value.code == 2
    assert "release stage" in capsys.readouterr().err
