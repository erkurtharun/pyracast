import errno
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy import host as helpers
from deploy import release_state
from tests.support.links import link

REPOSITORY = "us-docker.pkg.dev/project/repo"
YIELDED = "A restore rehearsal is running, so nothing was pruned; the next release prunes.\n"


def images(digit: str, *, web: str | None = None) -> dict[str, str]:
    values = {
        f"{name.upper()}_IMAGE": f"{REPOSITORY}/{name}@sha256:{digit * 64}"
        for name in ("backend", "web", "gateway", "postgres", "rabbitmq")
    }
    if web is not None:
        values["WEB_IMAGE"] = f"{REPOSITORY}/web@sha256:{web * 64}"
    return values


def four_images(digit: str) -> dict[str, str]:
    return {key: value for key, value in images(digit).items() if key != "RABBITMQ_IMAGE"}


class Host:
    def __init__(self, root: Path) -> None:
        self.releases = root / "releases"
        self.secrets = root / "secrets"
        self.current = root / "current"
        self.state = root / "release-state.json"
        self.removed: list[str] = []
        self.refused: set[str] = set()

    def release(self, name: str, values: dict[str, str]) -> Path:
        directory = self.releases / name
        (directory / "deploy").mkdir(parents=True)
        (directory / "deploy/release.env").write_text(
            "".join(f"{key}={value}\n" for key, value in values.items())
            + f"RUNTIME_ENV_FILE=/run/pyracast/releases/{name}/runtime.env\n"
        )
        (self.secrets / name).mkdir(parents=True)
        (self.secrets / name / "runtime.env").write_text("PGUSER=pyracast_app\n")
        return directory

    def remove_image(self, reference: str) -> None:
        if reference in self.refused:
            raise subprocess.CalledProcessError(1, ["docker", "image", "rm", reference])
        self.removed.append(reference)

    def prune(self) -> list[str]:
        return release_state.prune(
            self.state, self.releases, self.secrets, self.current, self.remove_image
        )


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


class Rehearsal:
    LOCK_EX = 2
    LOCK_NB = 4

    def __init__(self, lock: Path) -> None:
        self.lock = lock
        self.running = False
        self.error: OSError | None = None
        self.looks = 0
        self.on_look: Callable[[], None] = lambda: None

    def flock(self, _stream: object, operation: int) -> None:
        assert operation == self.LOCK_EX | self.LOCK_NB
        self.looks += 1
        self.on_look()
        if self.error is not None:
            raise self.error
        if self.running:
            raise BlockingIOError


@pytest.fixture
def command_line(host: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lock = tmp_path / "rehearsal.lock"
    monkeypatch.setattr(release_state, "STATE", host.state)
    monkeypatch.setattr(release_state, "RELEASES", host.releases)
    monkeypatch.setattr(release_state, "SECRETS", host.secrets)
    monkeypatch.setattr(release_state, "CURRENT", host.current)
    monkeypatch.setattr(helpers, "REHEARSAL_LOCK", lock)
    return lock


@pytest.fixture
def rehearsal(command_line: Path, monkeypatch: pytest.MonkeyPatch) -> Rehearsal:
    fake = Rehearsal(command_line)
    monkeypatch.setattr(
        helpers, "importlib", SimpleNamespace(import_module={"fcntl": fake}.__getitem__)
    )
    return fake


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    commands: list[list[str]] = []

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    return commands


def three_releases(host: Host) -> tuple[Path, Path, Path]:
    first = host.release("first", images("1"))
    second = host.release("second", images("2"))
    third = host.release("third", images("3"))
    return first, second, third


def test_record_names_the_serving_and_the_previous_release(host: Host) -> None:
    serving = host.release("b" * 40 + "-222222222222", images("2"))
    previous = host.release("a" * 40 + "-111111111111", images("1"))

    release_state.record(host.state, serving, previous)

    assert json.loads(host.state.read_text()) == {
        "current": serving.name,
        "previous": previous.name,
    }
    assert [path.name for path in host.state.parent.iterdir() if path.suffix == ".tmp"] == []


def test_prune_keeps_the_serving_and_the_previous_release_with_their_images(host: Host) -> None:
    oldest = host.release("oldest", images("1"))
    older = host.release("older", images("2", web="4"))
    previous = host.release("previous", images("3"))
    serving = host.release("serving", images("4"))
    release_state.record(host.state, serving, previous)

    assert host.prune() == []

    assert sorted(path.name for path in host.releases.iterdir()) == ["previous", "serving"]
    assert sorted(path.name for path in host.secrets.iterdir()) == ["previous", "serving"]
    kept = {*images("3").values(), *images("4").values()}
    expected = ({*images("1").values(), *images("2", web="4").values()}) - kept
    assert sorted(host.removed) == sorted(expected)
    assert f"{REPOSITORY}/web@sha256:{'4' * 64}" not in host.removed
    assert not oldest.exists()
    assert not older.exists()


def test_prune_removes_a_retired_broker_image_and_keeps_a_four_image_previous_release(
    host: Host,
) -> None:
    host.release("older", images("1"))
    previous = host.release("previous", four_images("2"))
    serving = host.release("serving", images("3"))
    release_state.record(host.state, serving, previous)

    assert host.prune() == []

    assert sorted(path.name for path in host.releases.iterdir()) == ["previous", "serving"]
    assert sorted(host.removed) == sorted(images("1").values())
    assert f"{REPOSITORY}/rabbitmq@sha256:{'1' * 64}" in host.removed


def test_prune_after_a_first_release_keeps_only_the_serving_release(host: Host) -> None:
    serving = host.release("serving", images("4"))
    release_state.record(host.state, serving, None)

    assert host.prune() == []

    assert [path.name for path in host.releases.iterdir()] == ["serving"]
    assert host.removed == []


def test_prune_also_keeps_the_release_that_current_points_to(host: Host) -> None:
    linked = host.release("linked", images("1"))
    previous = host.release("previous", images("2"))
    serving = host.release("serving", images("3"))
    release_state.record(host.state, serving, previous)

    assert (
        release_state.prune(
            host.state,
            host.releases,
            host.secrets,
            host.current,
            host.remove_image,
            resolve=lambda _link: linked,
        )
        == []
    )

    assert sorted(path.name for path in host.releases.iterdir()) == [
        "linked",
        "previous",
        "serving",
    ]


def test_prune_reports_an_image_it_cannot_remove_and_continues(host: Host) -> None:
    host.release("old", images("1"))
    previous = host.release("previous", images("2"))
    serving = host.release("serving", images("3"))
    release_state.record(host.state, serving, previous)
    refused = images("1")["BACKEND_IMAGE"]
    host.refused.add(refused)

    problems = host.prune()

    assert problems == [f"Image {refused} was not removed."]
    assert sorted(host.removed) == sorted(set(images("1").values()) - {refused})
    assert sorted(path.name for path in host.releases.iterdir()) == ["previous", "serving"]


@pytest.mark.parametrize(
    "content",
    [
        None,
        "not json",
        "[]",
        '{"current": "serving"}',
        '{"current": "../x", "previous": null}',
        '{"current": null, "previous": null}',
        '{"current": null, "previous": "old"}',
        '{"current": "serving", "previous": 7}',
    ],
)
def test_prune_without_a_valid_state_removes_nothing(host: Host, content: str | None) -> None:
    host.release("old", images("1"))
    host.release("serving", images("3"))
    if content is not None:
        host.state.write_text(content)

    problems = host.prune()

    assert problems == [f"{host.state} is missing or invalid; nothing was pruned."]
    assert sorted(path.name for path in host.releases.iterdir()) == ["old", "serving"]
    assert host.removed == []


def test_prune_removes_an_unkept_release_without_images_and_leaves_files(host: Host) -> None:
    previous = host.release("previous", images("2"))
    serving = host.release("serving", images("3"))
    (host.releases / "broken" / "deploy").mkdir(parents=True)
    (host.releases / "stray.txt").write_text("operator note\n")
    release_state.record(host.state, serving, previous)

    assert host.prune() == []

    assert sorted(path.name for path in host.releases.iterdir()) == [
        "previous",
        "serving",
        "stray.txt",
    ]
    assert host.removed == []


def test_command_line_records_and_prunes_with_docker(
    host: Host,
    rehearsal: Rehearsal,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    host.release("old", images("1"))
    previous = host.release("previous", images("2"))
    serving = host.release("serving", images("3"))
    commands: list[list[str]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[-1].endswith("1" * 64) and "/web@" in command[-1]:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)

    assert release_state.main(["record", str(serving), str(previous)]) == 0
    assert release_state.main(["prune"]) == 1

    assert sorted(command[-1] for command in commands) == sorted(images("1").values())
    assert all(command[:3] == ["docker", "image", "rm"] for command in commands)
    assert "was not removed" in capsys.readouterr().err
    assert rehearsal.looks == 1
    assert release_state.main(["record", str(serving)]) == 0
    assert json.loads(host.state.read_text())["previous"] is None


def test_two_promotions_during_a_rehearsal_keep_its_release_until_the_first_prune_after_it(
    host: Host,
    rehearsal: Rehearsal,
    docker: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    rehearsed, second, third = three_releases(host)
    rehearsal.running = True

    release_state.record(host.state, second, rehearsed)
    assert release_state.main(["prune"]) == 0
    release_state.record(host.state, third, second)
    assert release_state.main(["prune"]) == 0

    assert (rehearsed / "deploy/release.env").is_file()
    assert (host.secrets / rehearsed.name / "runtime.env").is_file()
    assert docker == []
    assert capsys.readouterr() == (YIELDED * 2, "")

    rehearsal.running = False
    assert release_state.main(["prune"]) == 0

    assert sorted(path.name for path in host.releases.iterdir()) == ["second", "third"]
    assert sorted(path.name for path in host.secrets.iterdir()) == ["second", "third"]
    assert sorted(command[-1] for command in docker) == sorted(images("1").values())
    assert capsys.readouterr() == ("", "")
    assert rehearsal.looks == 3


def test_a_rehearsal_that_starts_right_after_pruning_looked_binds_to_a_kept_release(
    host: Host, rehearsal: Rehearsal, docker: list[list[str]]
) -> None:
    retired, previous, serving = three_releases(host)
    link(serving, host.current)
    release_state.record(host.state, serving, previous)
    bound: list[Path] = []
    rehearsal.on_look = lambda: bound.append(host.current.resolve(strict=True))

    assert release_state.main(["prune"]) == 0

    assert bound == [serving.resolve()]
    assert (serving / "deploy/release.env").is_file()
    assert (host.secrets / serving.name / "runtime.env").is_file()
    assert not {command[-1] for command in docker} & set(images("3").values())
    assert not retired.exists()


def test_pruning_without_a_rehearsal_lock_file_creates_it_as_the_rehearsal_does(
    host: Host, rehearsal: Rehearsal, docker: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    retired, previous, serving = three_releases(host)
    release_state.record(host.state, serving, previous)
    opened: list[tuple[int, int]] = []
    real_open = os.open

    def recording_open(
        path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if Path(path) == rehearsal.lock:
            opened.append((flags, mode))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", recording_open)

    assert release_state.main(["prune"]) == 0

    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    assert opened == [(flags, 0o600)]
    assert rehearsal.lock.is_file()
    assert not retired.exists()
    assert sorted(command[-1] for command in docker) == sorted(images("1").values())


@pytest.mark.parametrize("case", ["missing directory", "directory", "lock error"])
def test_a_rehearsal_lock_that_cannot_be_opened_or_locked_never_stops_pruning(
    host: Host,
    rehearsal: Rehearsal,
    docker: list[list[str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    retired, previous, serving = three_releases(host)
    release_state.record(host.state, serving, previous)
    if case == "missing directory":
        monkeypatch.setattr(helpers, "REHEARSAL_LOCK", tmp_path / "missing" / "rehearsal.lock")
    elif case == "directory":
        rehearsal.lock.mkdir()
    else:
        rehearsal.error = OSError(errno.ENOLCK, "No locks available")

    assert release_state.main(["prune"]) == 0

    assert not retired.exists()
    assert sorted(path.name for path in host.releases.iterdir()) == ["second", "third"]
    assert sorted(command[-1] for command in docker) == sorted(images("1").values())
    assert not (tmp_path / "missing").exists()
    assert capsys.readouterr() == ("", "")


@pytest.mark.skipif(os.name == "nt", reason="flock exists only on the POSIX host")
def test_a_rehearsal_lock_held_by_another_descriptor_makes_pruning_yield_until_it_is_released(
    host: Host,
    command_line: Path,
    docker: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    rehearsed, second, third = three_releases(host)
    release_state.record(host.state, third, second)

    with helpers.open_lock(command_line) as held:
        assert helpers.try_lock(held)
        assert release_state.main(["prune"]) == 0
        assert rehearsed.is_dir()
        assert docker == []
    assert release_state.main(["prune"]) == 0
    with helpers.open_lock(command_line) as after:
        assert helpers.try_lock(after)

    assert not rehearsed.exists()
    assert capsys.readouterr() == (YIELDED, "")
