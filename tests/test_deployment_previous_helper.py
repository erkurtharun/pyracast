import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from deploy import reset_database

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
# The helper that recovers the first upgrade to this installer: deploy/reset_database.py on main
# at df15e76. Its application services are every service of its drain order.
FIRST_UPGRADE = "df15e76"
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


def installer() -> str:
    return (DEPLOY / "install-release.sh").read_text(encoding="utf-8")


def stop_path() -> str:
    script = installer()
    start = script.index("elif (( maintenance_started == 1 )); then")
    return script[start : script.index("if (( stopped == 1 )); then", start)]


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


def test_the_stop_path_keeps_the_notice_only_for_a_helper_that_runs_beside_it() -> None:
    running = left_running()

    assert {"gateway", "maintenance"} <= running["notice"]
    assert running["notice"] & reset_database.APPLICATION_SERVICES == set()
    assert running["stopped"] == {"postgres", "rabbitmq"}
    assert running["notice"] & first_upgrade_application() == {"gateway"}
    assert running["stopped"] & first_upgrade_application() == set()


def test_the_stop_path_says_which_gateway_state_it_left_and_whose_helper_recovers() -> None:
    script = installer()
    body = stop_path()

    assert "if [[ -z $previous ]] || helper_keeps_notice; then notice=1; fi" in body
    assert body.index("notice=1; fi") < body.index("if (( notice == 1 )); then")
    assert "The gateway is stopped too" in script
    assert "No release served before this one" in script
    assert "helper_keeps_notice() {" in script


def test_the_current_helper_accepts_the_notice_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "release-blocked"
    marker.write_text("identity")
    host = reset_database.Host(
        config=tmp_path / "host.json",
        release=tmp_path,
        bucket="bucket",
        runtime_version="1",
        marker=marker,
    )
    monkeypatch.setattr(reset_database, "running_services", lambda _host: left_running()["notice"])

    reset_database.require_maintenance(host)


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
