import time
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


class Locks:
    LOCK_EX = 2
    LOCK_NB = 4

    def __init__(self, holder: str | None) -> None:
        self.holder = holder
        self.calls: list[tuple[str, int]] = []

    def flock(self, target: object, operation: int) -> None:
        caller = "descriptor" if isinstance(target, int) else "probe"
        self.calls.append((caller, operation))
        if self.holder not in (None, caller):
            raise BlockingIOError


@pytest.fixture
def lock_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "release.lock"
    path.touch()
    monkeypatch.setattr(host, "RELEASE_LOCK", path)
    return path


def use_locks(monkeypatch: pytest.MonkeyPatch, locks: Locks) -> None:
    monkeypatch.setattr(
        host, "importlib", types.SimpleNamespace(import_module={"fcntl": locks}.__getitem__)
    )


@pytest.mark.parametrize(
    ("holder", "held"), [("descriptor", True), (None, False), ("another process", False)]
)
def test_only_a_descriptor_that_holds_the_release_lock_counts_as_holding_it(
    lock_file: Path, monkeypatch: pytest.MonkeyPatch, holder: str | None, held: bool
) -> None:
    locks = Locks(holder)
    use_locks(monkeypatch, locks)

    with lock_file.open("a") as stream:
        assert host.holds_release_lock(stream.fileno()) is held
    assert locks.calls[0] == ("probe", 6)


def test_a_descriptor_on_another_file_does_not_hold_the_release_lock(
    lock_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = Locks("descriptor")
    use_locks(monkeypatch, locks)

    with (tmp_path / "other.lock").open("a") as stream:
        assert not host.holds_release_lock(stream.fileno())
    assert locks.calls == []


def test_a_closed_descriptor_or_a_missing_lock_file_holds_nothing(
    lock_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = Locks("descriptor")
    use_locks(monkeypatch, locks)
    with lock_file.open("a") as stream:
        descriptor = stream.fileno()
    assert not host.holds_release_lock(descriptor)
    monkeypatch.setattr(host, "RELEASE_LOCK", tmp_path / "missing.lock")
    with lock_file.open("a") as stream:
        assert not host.holds_release_lock(stream.fileno())
    assert not (tmp_path / "missing.lock").exists()
    assert locks.calls == []


class Boot:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.boot_id = root / "boot_id"
        self.uptime = root / "uptime"
        monkeypatch.setattr(host, "BOOT_ID", self.boot_id)
        monkeypatch.setattr(host, "UPTIME", self.uptime)
        self.set("boot-a", 1000.0)

    def set(self, boot: str, uptime: float) -> None:
        self.boot_id.write_text(f"{boot}\n")
        self.uptime.write_text(f"{uptime:.2f} 3900.00\n")


@pytest.fixture
def boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Boot:
    return Boot(tmp_path, monkeypatch)


@pytest.mark.parametrize("step", [-30 * 86400, 0, 30 * 86400])
def test_a_mark_of_this_boot_ages_by_uptime_whatever_the_wall_clock_does(
    boot: Boot, monkeypatch: pytest.MonkeyPatch, step: float
) -> None:
    mark = host.clock_mark()
    boot.set("boot-a", 1600.0)
    wall = time.time() + step
    monkeypatch.setattr(time, "time", lambda: wall)

    assert mark == {"boot_id": "boot-a", "uptime_seconds": 1000.0}
    assert host.minimum_age(mark) == 600.0


def test_a_mark_of_an_earlier_boot_is_at_least_as_old_as_this_boot(boot: Boot) -> None:
    mark = host.clock_mark()
    boot.set("boot-b", 120.0)

    assert host.minimum_age(mark) == 120.0


@pytest.mark.parametrize(
    "mark",
    [
        None,
        "boot-a",
        [],
        {},
        {"boot_id": "boot-a"},
        {"boot_id": "boot-a", "uptime_seconds": "900"},
        {"boot_id": "boot-a", "uptime_seconds": 2000.0},
        {"boot_id": "boot-a", "uptime_seconds": -1.0},
    ],
)
def test_an_unreadable_or_impossible_mark_counts_as_old_as_this_boot(
    boot: Boot, mark: object
) -> None:
    assert host.minimum_age(mark) == 1000.0
