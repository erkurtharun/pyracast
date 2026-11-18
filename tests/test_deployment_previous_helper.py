import ast
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from deploy import release_record, reset_database

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
# The helper that recovers the first upgrade to this installer: deploy/reset_database.py on main
# at 7c7c272. Its application services are every service of its drain order.
FIRST_UPGRADE = "7c7c272"
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
PRERELEASE = "postgres/2026/11/18/20261118T103000Z-prerelease.dump"
ARCHIVE = "postgres/2026/11/18/20261118T090000Z-prereset-0123456789abcdef.dump"
# Runs one printed command through a helper's own argument parsing and guards, against a state
# in a temporary directory; reaching the first side effect (writing the secrets) is acceptance.
GUARDS = """
import contextlib, dataclasses, json, sys
from pathlib import Path
root, state, running, *arguments = sys.argv[1:]
sys.path.insert(0, root)
from deploy import reset_database as helper

class Accepted(BaseException):
    pass

def accept(host):
    raise Accepted

loaded = helper.load_host
helper.load_host = lambda config: dataclasses.replace(
    loaded(config), marker=Path(state, "release-blocked"), state=Path(state, "reset")
)
helper.release_lock = lambda host: contextlib.nullcontext()
helper.running_services = lambda host: set(json.loads(running))
helper.write_runtime_secrets = accept
try:
    helper.command_line(arguments)
except Accepted:
    sys.exit(0)
sys.exit(1)
"""
# Opens a maintenance session the way the helper's prepare and reset record it.
SESSION = """
import sys
from pathlib import Path
root, state, name, replaced = sys.argv[1:]
sys.path.insert(0, root)
from deploy import reset_database as helper
helper.sync = lambda path: None
host = helper.Host(
    config=Path(state, "host.json"), release=Path(root), bucket="pyracast-backups",
    runtime_version="1", marker=Path(state, "release-blocked"), state=Path(state, "reset"),
)
helper.write_synced(host.marker, "session-identity")
helper.save_session(host, helper.Session(
    marker="session-identity", object=name, release="previous", replaced=replaced == "replaced",
))
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


def main_source(name: str) -> str | None:
    result = subprocess.run(  # noqa: S603
        ["git", "show", f"{FIRST_UPGRADE}:deploy/{name}"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def first_upgrade_helper(root: Path) -> Path:
    # Read from git, never vendored; a shallow checkout (CI) lacks that history.
    sources = {name: main_source(name) for name in FIRST_UPGRADE_HELPER_FILES}
    if any(source is None for source in sources.values()):
        pytest.skip(f"Needs the history of main at {FIRST_UPGRADE}; CI checks out one commit.")
    (root / "deploy").mkdir(parents=True)
    for name, source in sources.items():
        (root / "deploy" / name).write_text(str(source), encoding="utf-8")
    return root


def helper_root(helper: str, tmp_path: Path) -> Path:
    return ROOT if helper == "current" else first_upgrade_helper(tmp_path / "main")


def recovery_tool() -> str:
    match = re.search(r"^recovery='([^']+)'$", installer(), re.MULTILINE)
    assert match is not None
    return match[1]


def maintenance_state(root: Path, state: Path, session: str | None) -> Path:
    state.mkdir()
    (state / "host.json").write_text(
        json.dumps({"backup_bucket": "pyracast-backups", "runtime_secret_version": "projects/p/1"}),
        encoding="utf-8",
    )
    if session is None:
        (state / "release-blocked").touch()
        return state
    name = ARCHIVE if session == "replaced" else ARCHIVE.replace("0123", "4567")
    subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", SESSION, str(root), str(state), name, session],
        check=True,
    )
    return state


def printed_recovery(state: Path, migrating: bool) -> str:
    # The installer's precedence: the reset session's archive, then this run's pre-release dump
    # once migrate began, otherwise abort.
    replaced = release_record.replaced_object(
        state / "reset" / "session.json", state / "release-blocked"
    )
    if replaced is not None:
        return f"{recovery_tool()} restore --confirm {replaced}"
    if migrating:
        return f"{recovery_tool()} restore --confirm {PRERELEASE}"
    return f"{recovery_tool()} abort"


def run_helper(root: Path, state: Path, command: str) -> subprocess.CompletedProcess[str]:
    arguments = shlex.split(command)
    assert arguments[:3] == [
        "python3",
        "/opt/pyracast/current/deploy/reset_database.py",
        "/etc/pyracast/host.json",
    ]
    running = left_running()["notice" if helper_keeps_notice(root) else "stopped"]
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-I",
            "-c",
            GUARDS,
            str(root),
            str(state),
            json.dumps(sorted(running)),
            str(state / "host.json"),
            *arguments[3:],
        ],
        capture_output=True,
        text=True,
        check=False,
    )


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


def test_the_installer_prints_one_recovery_per_state_in_the_tested_precedence() -> None:
    script = installer()
    commands = re.findall(r"\$recovery (abort|restore --confirm \$\w+)", script)
    order = [
        script.index("reset_object=$(python3"),
        script.index("elif (( migrating == 1 )); then"),
        script.index("$recovery restore --confirm $prerelease_object"),
        script.index("$recovery abort"),
    ]

    assert sorted(set(commands)) == [
        "abort",
        "restore --confirm $prerelease_object",
        "restore --confirm $reset_object",
    ]
    assert order == sorted(order)
    assert reset_database.Host(Path(), Path(), "", "").session == release_record.SESSION
    assert reset_database.Host(Path(), Path(), "", "").marker == release_record.MARKER


@pytest.mark.parametrize("helper", ["current", "main"])
@pytest.mark.parametrize(
    ("session", "migrating"),
    [
        (None, False),
        ("prepared", False),
        (None, True),
        ("replaced", False),
        ("replaced", True),
    ],
)
def test_the_recovery_printed_for_each_failure_state_is_accepted_by_the_helper_that_runs_it(
    tmp_path: Path, helper: str, session: str | None, migrating: bool
) -> None:
    root = helper_root(helper, tmp_path)
    state = maintenance_state(root, tmp_path / "host", session)

    result = run_helper(root, state, printed_recovery(state, migrating))

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("helper", ["current", "main"])
@pytest.mark.parametrize("command", ["abort", f"restore --confirm {PRERELEASE}"])
def test_after_a_reset_replaced_the_database_the_helper_accepts_only_its_archive(
    tmp_path: Path, helper: str, command: str
) -> None:
    root = helper_root(helper, tmp_path)
    state = maintenance_state(root, tmp_path / "host", "replaced")

    result = run_helper(root, state, f"{recovery_tool()} {command}")

    assert result.returncode == 1
    assert f"restore --confirm {ARCHIVE}" in result.stderr
    assert printed_recovery(state, migrating=True).endswith(f"restore --confirm {ARCHIVE}")


@pytest.mark.parametrize(
    ("session", "marker", "expected"),
    [
        ({"marker": "m", "object": ARCHIVE, "release": "r", "replaced": True}, "m\n", ARCHIVE),
        ({"marker": "m", "object": ARCHIVE, "release": "r", "replaced": False}, "m", None),
        ({"marker": "m", "object": ARCHIVE, "release": "r", "replaced": True}, "", None),
        ({"marker": "m", "object": ARCHIVE, "release": "r", "replaced": True}, None, None),
        ({"marker": "m", "object": ARCHIVE, "release": "r", "replaced": "yes"}, "m", None),
        ({"marker": "m", "object": "postgres/x.dump; touch /x", "replaced": True}, "m", None),
        ({"marker": "m", "object": "postgres/../x.dump", "replaced": True}, "m", None),
        ([ARCHIVE], "m", None),
        ("not json", "m", None),
        (None, "m", None),
    ],
)
def test_only_a_replaced_session_that_the_marker_still_holds_names_an_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    session: object,
    marker: str | None,
    expected: str | None,
) -> None:
    session_path = tmp_path / "session.json"
    marker_path = tmp_path / "release-blocked"
    if session is not None:
        content = session if isinstance(session, str) else json.dumps(session)
        session_path.write_text(content, encoding="utf-8")
    if marker is not None:
        marker_path.write_text(marker, encoding="utf-8")
    monkeypatch.setattr(release_record, "SESSION", session_path)
    monkeypatch.setattr(release_record, "MARKER", marker_path)

    assert release_record.replaced_object(session_path, marker_path) == expected
    assert release_record.main(["--replaced-by-reset"]) == (0 if expected else 1)
    assert capsys.readouterr().out == (f"{expected}\n" if expected else "")
    assert release_record.main([]) == 2
