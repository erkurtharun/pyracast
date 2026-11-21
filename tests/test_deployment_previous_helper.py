import ast
import contextlib
import dataclasses
import gzip
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

from deploy import release_record, reset_database

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
# The helper that recovers the first upgrade to this installer: deploy/reset_database.py on main
# at 7c7c272. Its application services are every service of its drain order.
FIRST_UPGRADE = "7c7c272"
# The helper of the release production serves when this one is installed: main at 4d94243c.
PREVIOUS_RELEASE = "4d94243c"
FIRST_UPGRADE_STOP_ORDER = (
    ("660", ("dispatcher",)),
    ("660", ("worker", "notifications")),
    ("100", ("gateway", "api", "web")),
)
FIRST_UPGRADE_HELPER_FILES = (
    "__init__.py",
    "reset_database.py",
    "backup.py",
    "host.py",
    "operations_log.py",
    "release_record.py",
)
ARCHIVE = "postgres/2026/11/18/20261118T090000Z-prereset-0123456789abcdef.dump"
REPLACED = {"marker": "m", "object": ARCHIVE, "release": "r", "replaced": True}
# Table counts of a database whose ledger the previous release serves ("current"), one that names
# migrations of a newer release ("newer") and a recreated, empty one.
COUNTS = {
    "current": "django_migrations 40\nplants_plant 3",
    "newer": "django_migrations 41\nplants_plant 3",
    "empty": "",
}
# Runs one command through a helper's own argument parsing, guards and steps against a model of
# the database: the database and every archive hold one of COUNTS' states, and the previous
# release's migrate --check passes only on "current". Exit status 0 with "served" written means
# the previous release serves again.
MODEL = """
import contextlib, dataclasses, json, sys
from pathlib import Path
root, state, running, *arguments = sys.argv[1:]
sys.path.insert(0, root)
from deploy import reset_database as helper
state = Path(state)
database = state / "database.json"
counts = json.loads((state / "counts.json").read_text())

def ledger():
    return json.loads(database.read_text())

def load(value):
    database.write_text(json.dumps(value))

def archived(archive):
    return json.loads(archive.read_text())

@contextlib.contextmanager
def throwaway_copy(host, archive):
    yield lambda sql: counts[archived(archive)]

def download_archive(host, name):
    archive = helper.archive_path(host, name)
    archive.write_bytes((state / "bucket" / name).read_bytes())
    return archive

def start_release(host):
    (state / "served").write_text(ledger())
    host.marker.unlink()

loaded = helper.load_host
helper.load_host = lambda config: dataclasses.replace(
    loaded(config), marker=state / "release-blocked", state=state / "reset"
)
helper.release_lock = lambda host: contextlib.nullcontext()
helper.running_services = lambda host: set(json.loads(running))
helper.sync = lambda path: None
for name in (
    "write_runtime_secrets", "start_support", "require_roles", "purge_queues", "provision",
    "check_roles",
):
    setattr(helper, name, lambda host: None)
helper.download_archive = download_archive
helper.throwaway_copy = throwaway_copy
helper.recreate_database = lambda host: load("empty")
helper.restore_live = lambda host, archive: load(archived(archive))
helper.table_counts = lambda host: helper.parse_counts(counts[ledger()])
helper.release_serves = lambda host: ledger() == "current"
helper.start_release = start_release
sys.exit(helper.command_line(arguments))
"""
# Reads the session as a helper's own guards read it.
READ = """
import dataclasses, json, sys
from pathlib import Path
root, state = sys.argv[1:]
sys.path.insert(0, root)
from deploy import reset_database as helper
host = helper.Host(
    config=Path(state, "host.json"), release=Path(root), bucket="pyracast-backups",
    runtime_version="1", marker=Path(state, "release-blocked"), state=Path(state, "reset"),
)
print(json.dumps(dataclasses.asdict(helper.current_session(host))))
"""


def installer() -> str:
    return (DEPLOY / "install-release.sh").read_text(encoding="utf-8")


def function_body(name: str) -> str:
    script = installer()
    start = script.index(f"\n{name}() {{\n")
    return script[start : script.index("\n}\n", start) + 1]


def stop_path() -> str:
    return function_body("stop_application")


def stopped(lines: str) -> set[str]:
    return {
        service
        for match in re.finditer(
            r'"\$\{compose\[@\]\}" stop -t \d+ ([a-z ]+) \|\| stopped=0', lines
        )
        for service in match[1].split()
    }


def long_running_services() -> set[str]:
    services = yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))["services"]
    return {name for name, service in services.items() if "profiles" not in service}


def left_running() -> dict[str, set[str]]:
    body = stop_path()
    branch = body.index("if (( notice == 1 )); then")
    otherwise = body.index("else", branch)
    end = body.index("fi\n", otherwise)
    common = stopped(body[:branch])
    return {
        "notice": long_running_services() - common - stopped(body[branch:otherwise]),
        "stopped": long_running_services() - common - stopped(body[otherwise:end]),
    }


def first_upgrade_application() -> frozenset[str]:
    return frozenset(service for _, group in FIRST_UPGRADE_STOP_ORDER for service in group)


def notice_check() -> str:
    match = re.search(r"python3 -I -c '([^']+)' /opt/pyracast/current", installer())
    assert match is not None
    return match[1]


def helper_keeps_notice(release: Path) -> bool:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", notice_check(), str(release)],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def main_source(name: str, commit: str = FIRST_UPGRADE) -> str | None:
    result = subprocess.run(  # noqa: S603
        ["git", "show", f"{commit}:deploy/{name}"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def first_upgrade_helper(root: Path, commit: str = FIRST_UPGRADE) -> Path:
    # Read from git, never vendored; a shallow checkout (CI) lacks that history.
    sources = {name: main_source(name, commit) for name in FIRST_UPGRADE_HELPER_FILES}
    if any(source is None for source in sources.values()):
        pytest.skip(f"Needs the history of main at {commit}; CI checks out one commit.")
    (root / "deploy").mkdir(parents=True)
    for name, source in sources.items():
        (root / "deploy" / name).write_text(str(source), encoding="utf-8")
    return root


def helper_root(helper: str, tmp_path: Path) -> Path:
    if helper == "current":
        return ROOT
    commit = PREVIOUS_RELEASE if helper == "previous" else FIRST_UPGRADE
    return first_upgrade_helper(tmp_path / helper, commit)


class Maintenance:
    """A host during one maintenance, as the installer and the helper leave it."""

    def __init__(self, state: Path) -> None:
        state.mkdir()
        (state / "host.json").write_text(
            json.dumps({"backup_bucket": "pyracast-backups", "runtime_secret_version": "1"}),
            encoding="utf-8",
        )
        (state / "counts.json").write_text(json.dumps(COUNTS), encoding="utf-8")
        self.state = state
        self.host = reset_database.Host(
            config=Path("/etc/pyracast/host.json"),
            release=Path("/opt/pyracast/current"),
            bucket="pyracast-backups",
            runtime_version="1",
            marker=state / "release-blocked",
            state=state / "reset",
        )
        self.runs: list[str] = []
        self.load("current")

    def load(self, ledger: str) -> None:
        (self.state / "database.json").write_text(json.dumps(ledger), encoding="utf-8")

    def ledger(self) -> str:
        return str(json.loads((self.state / "database.json").read_text(encoding="utf-8")))

    def prepare(self) -> None:
        identity = "prepared-identity"
        reset_database.write_synced(self.host.marker, identity)
        self.host.state.mkdir(exist_ok=True)
        archive = reset_database.archive_path(self.host, ARCHIVE)
        archive.write_text(json.dumps(self.ledger()), encoding="utf-8")
        export = reset_database.export_name(ARCHIVE)
        exported = reset_database.archive_path(self.host, export)
        exported.write_bytes(gzip.compress(b""))
        solcast = reset_database.SolcastExport(
            object=export, sha256=reset_database.file_sha256(exported), rows=0
        )
        record = reset_database.Record(
            object=ARCHIVE,
            sha256=reset_database.file_sha256(archive),
            counts=reset_database.parse_counts(COUNTS[self.ledger()]),
            solcast=solcast,
        )
        reset_database.save_record(self.host, record)
        session = reset_database.Session(
            marker=identity, object=ARCHIVE, release="previous", replaced=False
        )
        reset_database.save_session(self.host, session)

    def reset(self) -> None:
        session = reset_database.current_session(self.host)
        assert session is not None
        reset_database.save_session(self.host, dataclasses.replace(session, replaced=True))
        self.load("empty")

    @property
    def record(self) -> Path:
        return self.state / "prerelease-backup.json"

    def release(self, *, migrates: bool) -> str:
        """One installer run: the marker, its pre-release dump and, before migrate, its record.

        backup.py uploads the dump and keeps it where the recovery helper looks for an archive.
        """
        if not self.host.marker.exists():
            self.host.marker.touch()
        name = f"postgres/2026/11/18/20261118T1{len(self.runs)}0000Z-prerelease-run.dump"
        dump = self.state / "bucket" / name
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(json.dumps(self.ledger()), encoding="utf-8")
        self.host.state.mkdir(exist_ok=True)
        shutil.copyfile(dump, reset_database.archive_path(self.host, name))
        counts = reset_database.parse_counts(COUNTS[self.ledger()])
        self.record.write_text(json.dumps({"object": name, "counts": counts}), encoding="utf-8")
        self.runs.append(name)
        if migrates:
            release_record.record_session(self.host, self.record)
            self.load("newer")
        return name

    def kept(self) -> list[str]:
        return sorted(path.name for path in self.host.state.iterdir())


# Each maintenance as the events that left it, and the object that holds what the previous
# release served once the database changed (None: unchanged, recovered with abort).
SCENARIOS: dict[str, tuple[tuple[str, ...], str | None]] = {
    "release failed before migrate": (("fails",), None),
    "release failed in migrate": (("migrates",), "first run"),
    "retry failed before its migrate": (("migrates", "fails"), "first run"),
    "retry failed in its migrate": (("migrates", "migrates"), "first run"),
    "prepared, release failed before migrate": (("prepare", "fails"), None),
    "prepared, release failed in migrate": (("prepare", "migrates"), "archive"),
    "reset, release failed before migrate": (("prepare", "reset", "fails"), "archive"),
    "reset, release failed in migrate": (("prepare", "reset", "migrates"), "archive"),
}


def maintenance_after(state: Path, scenario: str) -> tuple[Maintenance, str | None]:
    maintenance = Maintenance(state)
    events, kept = SCENARIOS[scenario]
    for event in events:
        match event:
            case "prepare":
                maintenance.prepare()
            case "reset":
                maintenance.reset()
            case _:
                maintenance.release(migrates=event == "migrates")
    if kept is None:
        return maintenance, None
    return maintenance, ARCHIVE if kept == "archive" else maintenance.runs[0]


def run_helper(root: Path, state: Path, arguments: str) -> subprocess.CompletedProcess[str]:
    running = left_running()["notice" if helper_keeps_notice(root) else "stopped"]
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-I",
            "-c",
            MODEL,
            str(root),
            str(state),
            json.dumps(sorted(running)),
            str(state / "host.json"),
            *arguments.split(),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@contextlib.contextmanager
def counted_copy(_host: reset_database.Host, archive: Path) -> Iterator[Callable[[str], str]]:
    yield lambda _sql: COUNTS[json.loads(archive.read_text(encoding="utf-8"))]


@pytest.fixture(autouse=True)
def unsynced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reset_database, "sync", lambda path: None)
    monkeypatch.setattr(reset_database, "throwaway_copy", counted_copy)


def test_the_stop_path_keeps_the_notice_only_for_a_helper_that_runs_beside_it() -> None:
    running = left_running()

    assert {"gateway", "maintenance"} <= running["notice"]
    assert running["notice"] & reset_database.APPLICATION_SERVICES == set()
    assert running["stopped"] == {"postgres", "rabbitmq"}
    assert running["notice"] & first_upgrade_application() == {"gateway"}
    assert running["stopped"] & first_upgrade_application() == set()


def test_the_stop_path_asks_the_helper_before_it_chooses_the_gateway_state() -> None:
    cleanup = function_body("cleanup")

    assert "if [[ -z $previous ]] || helper_keeps_notice; then notice=1; fi" in cleanup
    assert cleanup.index("notice=1; fi") < cleanup.index("      stop_application\n")
    assert "if (( notice == 1 )); then" in stop_path()


def test_the_installer_asks_the_previous_helper_whether_the_notice_may_stay(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    shutil.copytree(DEPLOY, current / "deploy", ignore=shutil.ignore_patterns("__pycache__"))
    legacy = tmp_path / "legacy" / "deploy"
    legacy.mkdir(parents=True)
    (legacy / "reset_database.py").write_text(
        f"STOP_ORDER = {FIRST_UPGRADE_STOP_ORDER!r}\n"
        "APPLICATION_SERVICES = frozenset(s for _, group in STOP_ORDER for s in group)\n",
        encoding="utf-8",
    )

    assert helper_keeps_notice(current)
    assert not helper_keeps_notice(legacy.parent)
    assert not helper_keeps_notice(tmp_path / "missing")


def test_main_helper_at_the_first_upgrade_has_the_recorded_drain_order(tmp_path: Path) -> None:
    helper = first_upgrade_helper(tmp_path / "main")
    tree = ast.parse((helper / "deploy" / "reset_database.py").read_text(encoding="utf-8"))
    (order,) = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "STOP_ORDER" for target in node.targets
        )
    ]

    assert ast.literal_eval(order) == FIRST_UPGRADE_STOP_ORDER
    assert not helper_keeps_notice(helper)


@pytest.mark.parametrize(("state", "accepted"), [("stopped", True), ("notice", False)])
def test_main_helper_guards_judge_the_state_the_stop_path_leaves(
    tmp_path: Path, state: str, accepted: bool
) -> None:
    helper = first_upgrade_helper(tmp_path / "main")
    marker = tmp_path / "release-blocked"
    marker.write_text("identity")
    driver = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from deploy import reset_database as helper\n"
        "helper.running_services = lambda _host: set(sys.argv[3].split())\n"
        "host = helper.Host(config=Path('host.json'), release=Path(sys.argv[1]),\n"
        "                   bucket='bucket', runtime_version='1', marker=Path(sys.argv[2]))\n"
        "try:\n"
        "    helper.require_maintenance(host)\n"
        "except helper.RefusedError as error:\n"
        "    sys.exit(str(error))\n"
    )

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-I",
            "-c",
            driver,
            str(helper),
            str(marker),
            " ".join(sorted(left_running()[state])),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert (result.returncode == 0) is accepted, result.stderr
    if not accepted:
        assert "Application containers still run: gateway." in result.stderr


@pytest.mark.parametrize("helper", ["current", "main"])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_printed_recovery_serves_what_the_previous_release_served(
    tmp_path: Path, helper: str, scenario: str
) -> None:
    root = helper_root(helper, tmp_path)
    maintenance, kept = maintenance_after(tmp_path / "host", scenario)
    arguments = "abort" if kept is None else f"restore --confirm {kept}"
    printed = release_record.recovery(maintenance.host)

    result = run_helper(root, maintenance.state, arguments)

    assert maintenance.host.command(arguments) in printed
    assert printed.count("reset_database.py") == 1
    assert result.returncode == 0, result.stderr
    assert (maintenance.state / "served").read_text(encoding="utf-8") == "current"
    assert not maintenance.host.marker.exists()


@pytest.mark.parametrize("helper", ["current", "main"])
@pytest.mark.parametrize("scenario", [name for name, (_, kept) in SCENARIOS.items() if kept])
def test_once_the_database_changed_the_helper_refuses_every_other_recovery(
    tmp_path: Path, helper: str, scenario: str
) -> None:
    root = helper_root(helper, tmp_path)
    maintenance, kept = maintenance_after(tmp_path / "host", scenario)
    session = maintenance.host.session.read_bytes()
    ledger = maintenance.ledger()
    others = [f"restore --confirm {name}" for name in maintenance.runs if name != kept]

    for arguments in ["abort", *others]:
        result = run_helper(root, maintenance.state, arguments)

        assert result.returncode == 2, (arguments, result.stderr)
        assert f"restore --confirm {kept}" in result.stderr
        assert maintenance.host.session.read_bytes() == session
        assert maintenance.ledger() == ledger
        assert not (maintenance.state / "served").exists()


@pytest.mark.parametrize("helper", ["current", "main"])
@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps([ARCHIVE]),
        json.dumps({key: REPLACED[key] for key in ("marker", "object", "replaced")}),
        json.dumps(REPLACED | {"x": 1}),
        json.dumps(REPLACED | {"object": "postgres/x.dump; touch /x"}),
    ],
)
def test_an_unreadable_session_is_named_and_no_recovery_is_printed(
    tmp_path: Path, helper: str, content: str
) -> None:
    root = helper_root(helper, tmp_path)
    maintenance = Maintenance(tmp_path / "host")
    maintenance.host.marker.write_text("m", encoding="utf-8")
    maintenance.host.state.mkdir()
    maintenance.host.session.write_text(content, encoding="utf-8")

    printed = release_record.recovery(maintenance.host)

    assert str(maintenance.host.session) in printed
    assert "reset_database.py" not in printed
    assert "touch" not in printed
    for arguments in ("abort", f"restore --confirm {ARCHIVE}"):
        result = run_helper(root, maintenance.state, arguments)
        assert result.returncode != 0, (arguments, result.stderr)
        assert not (maintenance.state / "served").exists()


@pytest.mark.parametrize(
    ("marker", "identity"), [("", None), ("reset-session\n", "reset-session"), ("stale", "stale")]
)
def test_the_record_names_this_runs_dump_when_no_session_holds_the_marker(
    tmp_path: Path, marker: str, identity: str | None
) -> None:
    maintenance = Maintenance(tmp_path / "host")
    maintenance.host.marker.write_text(marker, encoding="utf-8")
    ended = reset_database.Session(marker="ended", object=ARCHIVE, release="old", replaced=True)
    reset_database.save_session(maintenance.host, ended)

    name = maintenance.release(migrates=True)

    held = maintenance.host.marker.read_text(encoding="utf-8").strip()
    assert held == identity if identity else re.fullmatch(r"[0-9a-f]{32}", held)
    assert json.loads(maintenance.host.session.read_text(encoding="utf-8")) == {
        "marker": held,
        "object": name,
        "release": "current",
        "replaced": True,
    }


def test_the_record_keeps_a_prepared_reset_archive_and_marks_it_replaced(tmp_path: Path) -> None:
    maintenance = Maintenance(tmp_path / "host")
    maintenance.prepare()

    maintenance.release(migrates=True)

    assert json.loads(maintenance.host.session.read_text(encoding="utf-8")) == {
        "marker": "prepared-identity",
        "object": ARCHIVE,
        "release": "previous",
        "replaced": True,
    }


def test_the_record_leaves_a_replaced_session_unchanged(tmp_path: Path) -> None:
    maintenance = Maintenance(tmp_path / "host")
    maintenance.release(migrates=True)
    session = maintenance.host.session.read_bytes()
    marker = maintenance.host.marker.read_bytes()

    kept = release_record.record_session(maintenance.host, tmp_path / "missing.json")

    assert kept.object == maintenance.runs[0]
    assert maintenance.host.session.read_bytes() == session
    assert maintenance.host.marker.read_bytes() == marker


@pytest.mark.parametrize(
    "record", [None, {"object": "postgres/x.dump; touch /x"}, {"object": "postgres/../x.dump"}]
)
def test_the_record_refuses_without_a_valid_pre_release_object_and_writes_nothing(
    tmp_path: Path, record: dict[str, str] | None
) -> None:
    maintenance = Maintenance(tmp_path / "host")
    maintenance.host.marker.touch()
    path = tmp_path / "prerelease-backup.json"
    if record is not None:
        path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="pre-release object"):
        release_record.record_session(maintenance.host, path)

    assert maintenance.host.marker.read_text(encoding="utf-8") == ""
    assert not maintenance.host.session.exists()


def test_the_record_refuses_an_unreadable_session_and_leaves_it(tmp_path: Path) -> None:
    maintenance = Maintenance(tmp_path / "host")
    maintenance.host.marker.write_text("m", encoding="utf-8")
    maintenance.host.state.mkdir()
    maintenance.host.session.write_text("not json", encoding="utf-8")
    record = tmp_path / "prerelease-backup.json"
    record.write_text(json.dumps({"object": ARCHIVE}), encoding="utf-8")

    with pytest.raises(ValueError, match="Expecting value"):
        release_record.record_session(maintenance.host, record)

    assert maintenance.host.session.read_text(encoding="utf-8") == "not json"


@pytest.mark.parametrize("helper", ["current", "main"])
def test_both_helpers_read_the_session_the_installer_records(tmp_path: Path, helper: str) -> None:
    root = helper_root(helper, tmp_path)
    maintenance = Maintenance(tmp_path / "host")
    maintenance.release(migrates=True)

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", READ, str(root), str(maintenance.state)],
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout) == json.loads(
        maintenance.host.session.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("helper", ["current", "main", "previous"])
@pytest.mark.parametrize("scenario", ["release failed in migrate", "retry failed in its migrate"])
def test_the_printed_restore_serves_from_the_kept_archive_once_the_bucket_lost_the_dump(
    tmp_path: Path, helper: str, scenario: str
) -> None:
    root = helper_root(helper, tmp_path)
    maintenance, kept = maintenance_after(tmp_path / "host", scenario)
    shutil.rmtree(maintenance.state / "bucket")

    result = run_helper(root, maintenance.state, f"restore --confirm {kept}")

    assert result.returncode == 0, result.stderr
    assert (maintenance.state / "served").read_text(encoding="utf-8") == "current"


def test_the_record_keeps_the_dump_with_the_counts_of_its_throwaway_restore(
    tmp_path: Path,
) -> None:
    maintenance = Maintenance(tmp_path / "host")

    name = maintenance.release(migrates=True)

    archive = reset_database.archive_path(maintenance.host, name)
    record = reset_database.load_record(maintenance.host, name)
    assert record == reset_database.Record(
        object=name,
        sha256=reset_database.file_sha256(archive),
        counts={"django_migrations": 40, "plants_plant": 3},
    )
    assert maintenance.kept() == sorted(
        [archive.name, archive.with_suffix(".json").name, "session.json"]
    )


@pytest.mark.parametrize(
    ("ledger", "dumped", "message"),
    [
        ("empty", None, "holds no applied migration"),
        ("current", {"django_migrations": 40, "plants_plant": 4}, "row counts differ"),
    ],
)
def test_the_record_refuses_a_dump_no_release_could_serve_and_keeps_nothing(
    tmp_path: Path, ledger: str, dumped: dict[str, int] | None, message: str
) -> None:
    maintenance = Maintenance(tmp_path / "host")
    maintenance.load(ledger)
    name = maintenance.release(migrates=False)
    if dumped is not None:
        maintenance.record.write_text(json.dumps({"object": name, "counts": dumped}))

    with pytest.raises((ValueError, RuntimeError), match=message):
        release_record.record_session(maintenance.host, maintenance.record)

    assert maintenance.host.marker.read_text(encoding="utf-8") == ""
    assert maintenance.kept() == []


def test_a_dump_the_throwaway_cannot_restore_is_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    maintenance = Maintenance(tmp_path / "host")
    maintenance.release(migrates=False)

    def broken(
        _host: reset_database.Host, archive: Path
    ) -> contextlib.AbstractContextManager[Callable[[str], str]]:
        raise subprocess.CalledProcessError(1, ["pg_restore"], stderr=archive.name)

    monkeypatch.setattr(reset_database, "throwaway_copy", broken)

    with pytest.raises(subprocess.CalledProcessError):
        release_record.record_session(maintenance.host, maintenance.record)

    assert maintenance.kept() == []


def test_a_retry_drops_its_own_dump_and_keeps_the_first_runs_recovery_point(
    tmp_path: Path,
) -> None:
    maintenance = Maintenance(tmp_path / "host")
    first = maintenance.release(migrates=True)
    files = maintenance.kept()

    maintenance.release(migrates=True)

    assert maintenance.kept() == files
    assert reset_database.archive_path(maintenance.host, first).name in files


def test_a_prepared_reset_keeps_its_archive_and_the_release_drops_its_dump(tmp_path: Path) -> None:
    maintenance = Maintenance(tmp_path / "host")
    maintenance.prepare()
    files = maintenance.kept()

    maintenance.release(migrates=True)

    assert maintenance.kept() == files


def state_after_maintenances(tmp_path: Path) -> Maintenance:
    maintenance = Maintenance(tmp_path / "host")
    maintenance.prepare()
    maintenance.host.marker.unlink()
    maintenance.release(migrates=True)
    reset_database.archive_path(maintenance.host, maintenance.runs[0]).with_suffix(
        ".partial"
    ).write_bytes(b"")
    return maintenance


def test_the_end_of_a_successful_release_removes_its_pre_release_archives_and_the_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    maintenance = state_after_maintenances(tmp_path)
    monkeypatch.setattr(reset_database, "load_host", lambda config: maintenance.host)
    maintenance.host.marker.unlink()

    assert release_record.main(["/etc/pyracast/host.json", "end"]) == 0

    assert all("-prerelease-" not in name for name in maintenance.kept())
    assert maintenance.kept() == sorted(
        [
            reset_database.archive_path(maintenance.host, ARCHIVE).name,
            reset_database.record_path(maintenance.host, ARCHIVE).name,
            reset_database.archive_path(maintenance.host, reset_database.export_name(ARCHIVE)).name,
        ]
    )
    assert str(maintenance.host.state) in capsys.readouterr().out


def test_the_end_step_leaves_an_open_maintenance_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    maintenance = state_after_maintenances(tmp_path)
    monkeypatch.setattr(reset_database, "load_host", lambda config: maintenance.host)
    files = maintenance.kept()

    assert release_record.main(["/etc/pyracast/host.json", "end"]) == 1

    assert maintenance.kept() == files
    assert str(maintenance.host.marker) in capsys.readouterr().err


def test_the_end_step_on_a_host_without_kept_files_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    maintenance = Maintenance(tmp_path / "host")
    monkeypatch.setattr(reset_database, "load_host", lambda config: maintenance.host)

    assert release_record.main(["/etc/pyracast/host.json", "end"]) == 0
    assert not maintenance.host.state.exists()


def prerelease_files(maintenance: Maintenance) -> list[str]:
    return [name for name in maintenance.kept() if "-prerelease-" in name]


def test_discarding_without_a_held_session_removes_every_pre_release_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    maintenance = state_after_maintenances(tmp_path)
    monkeypatch.setattr(reset_database, "load_host", lambda config: maintenance.host)
    maintenance.host.marker.unlink()
    maintenance.release(migrates=False)
    session = maintenance.host.session.read_bytes()

    assert release_record.main(["/etc/pyracast/host.json", "discard"]) == 0

    assert prerelease_files(maintenance) == []
    assert reset_database.archive_path(maintenance.host, ARCHIVE).name in maintenance.kept()
    assert maintenance.host.session.read_bytes() == session
    assert str(maintenance.host.state) in capsys.readouterr().out


def test_discarding_keeps_the_dump_the_held_session_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    maintenance = cli_host(tmp_path, monkeypatch)
    first = maintenance.release(migrates=True)
    kept = maintenance.kept()
    maintenance.release(migrates=False)
    reset_database.archive_path(maintenance.host, first).with_suffix(".partial").write_bytes(b"")

    assert release_record.main(["/etc/pyracast/host.json", "discard"]) == 0

    assert maintenance.kept() == kept
    assert reset_database.archive_path(maintenance.host, first).name in kept


def test_discarding_beside_an_unreadable_session_removes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    maintenance = cli_host(tmp_path, monkeypatch)
    maintenance.release(migrates=False)
    maintenance.host.marker.write_text("m", encoding="utf-8")
    maintenance.host.session.write_text("not json", encoding="utf-8")
    files = maintenance.kept()

    assert release_record.main(["/etc/pyracast/host.json", "discard"]) == 1

    assert maintenance.kept() == files
    assert str(maintenance.host.session) in capsys.readouterr().err


def written_then_failed(path: Path) -> None:
    if path.name == "session.json":
        raise subprocess.CalledProcessError(1, ["sync", "-f", str(path)], stderr="disk failed")


@pytest.mark.parametrize(
    "failure", ["record", "marker", "session", "session synced", "session unreadable"]
)
def test_a_recovery_point_that_cannot_be_recorded_keeps_only_a_dump_the_session_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    maintenance = Maintenance(tmp_path / "host")
    name = maintenance.release(migrates=False)

    def failed(*_arguments: object) -> None:
        raise OSError("No space left on device")

    match failure:
        case "record":
            monkeypatch.setattr(reset_database, "save_record", failed)
        case "marker":
            monkeypatch.setattr(reset_database, "marker_identity", failed)
        case "session":
            monkeypatch.setattr(reset_database, "save_session", failed)
        case "session unreadable":

            def torn(host: reset_database.Host, _session: reset_database.Session) -> None:
                host.session.write_text("not json", encoding="utf-8")
                raise OSError("No space left on device")

            monkeypatch.setattr(reset_database, "save_session", torn)
        case _:
            monkeypatch.setattr(reset_database, "sync", written_then_failed)

    with pytest.raises((OSError, subprocess.CalledProcessError)):
        release_record.record_session(maintenance.host, maintenance.record)

    if failure == "session unreadable":
        assert reset_database.archive_path(maintenance.host, name).name in maintenance.kept()
    elif failure == "session synced":
        archive = reset_database.archive_path(maintenance.host, name)
        assert maintenance.kept() == sorted(
            [archive.name, archive.with_suffix(".json").name, "session.json"]
        )
    else:
        assert prerelease_files(maintenance) == []


def cli_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Maintenance:
    maintenance = Maintenance(tmp_path / "host")
    monkeypatch.setattr(reset_database, "load_host", lambda config: maintenance.host)
    return maintenance


def test_the_record_command_prints_the_recovery_point_and_the_recovery_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    maintenance = cli_host(tmp_path, monkeypatch)
    name = maintenance.release(migrates=False)
    record = str(maintenance.record)

    assert release_record.main(["/etc/pyracast/host.json", "record", record]) == 0
    assert name in capsys.readouterr().out
    assert release_record.main(["/etc/pyracast/host.json", "recovery"]) == 0
    assert maintenance.host.command(f"restore --confirm {name}") in capsys.readouterr().out
    assert release_record.main(["/etc/pyracast/host.json"]) == 2


def test_a_record_that_cannot_be_written_fails_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    maintenance = cli_host(tmp_path, monkeypatch)
    maintenance.release(migrates=False)
    record = maintenance.record

    def failing_sync(path: Path) -> None:
        raise subprocess.CalledProcessError(1, ["sync", "-f", str(path)], stderr="disk failed")

    monkeypatch.setattr(reset_database, "sync", failing_sync)

    assert release_record.main(["/etc/pyracast/host.json", "record", str(record)]) == 1
    assert "disk failed" in capsys.readouterr().err


def test_the_recovery_command_names_an_unreadable_host_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "host.json"
    config.write_text("{}", encoding="utf-8")

    assert release_record.main([str(config), "recovery"]) == 1
    assert release_record.main([str(config), "record", str(tmp_path / "record.json")]) == 1
    error = capsys.readouterr().err
    assert str(config) in error
    assert "backup_bucket" in error
