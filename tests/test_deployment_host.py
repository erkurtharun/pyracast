import types
from pathlib import Path

import pytest

from deploy import host


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(host, "RELEASE_LOCK", tmp_path / "release.lock")
    monkeypatch.setattr(
        host, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    )
    return clock


def test_free_release_lock_is_granted_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    monkeypatch.setattr(host, "try_lock", lambda _stream: True)
    with host.release_lock(600) as locked:
        assert locked
    assert clock.sleeps == []
    assert (tmp_path / "release.lock").exists()


def test_release_lock_waits_until_the_holder_releases_it(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    attempts = iter([False, False, True])
    monkeypatch.setattr(host, "try_lock", lambda _stream: next(attempts))
    with host.release_lock(600) as locked:
        assert locked
    assert clock.sleeps == [1, 1]


@pytest.mark.parametrize(("wait", "sleeps"), [(0, []), (3, [1, 1, 1])])
def test_release_lock_gives_up_at_its_deadline(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, wait: float, sleeps: list[float]
) -> None:
    monkeypatch.setattr(host, "try_lock", lambda _stream: False)
    with host.release_lock(wait) as locked:
        assert not locked
    assert clock.sleeps == sleeps


def test_pointer_lock_waits_for_an_exclusive_lock_beside_the_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations: list[int] = []
    fcntl = types.SimpleNamespace(
        LOCK_EX=2, LOCK_NB=4, flock=lambda _stream, operation: operations.append(operation)
    )
    monkeypatch.setattr(
        host, "importlib", types.SimpleNamespace(import_module={"fcntl": fcntl}.__getitem__)
    )
    monkeypatch.setattr(host, "LAST_BACKUP", tmp_path / "last-backup.json")

    with host.pointer_lock():
        assert operations == [2]
    assert (tmp_path / "last-backup.lock").exists()


@pytest.mark.parametrize("busy", [False, True])
def test_lock_attempt_is_exclusive_and_never_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, busy: bool
) -> None:
    operations: list[int] = []

    def flock(_stream: object, operation: int) -> None:
        operations.append(operation)
        if busy:
            raise BlockingIOError

    fcntl = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=flock)
    monkeypatch.setattr(
        host, "importlib", types.SimpleNamespace(import_module={"fcntl": fcntl}.__getitem__)
    )
    with (tmp_path / "release.lock").open("a") as stream:
        assert host.try_lock(stream) is not busy
    assert operations == [6]
