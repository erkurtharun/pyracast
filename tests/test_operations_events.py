import ast
import io
import json
import re
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from deploy import operations_events, operations_log
from tests.support.log_filters import filter_of, filter_terms, matches, resources

EVENTS = operations_events.EVENTS
ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
TEMPLATES = ROOT / "infra" / "terraform" / "templates"
MONITORING = ROOT / "infra" / "terraform" / "monitoring.tf"
VARIABLES = ROOT / "infra" / "terraform" / "variables.tf"
RUNBOOK = ROOT / "docs" / "DEPLOYMENT.md"
# An operations line carries ids, states and counts, never who or what a customer is.
IDENTIFYING = re.compile(r"mail|address|name|key|domain|site|host|url|message|stderr|stdout")
# Each call that passes its fields as a mapping, with the functions whose returned dict literals
# are that mapping; a new such call fails the scan until its keys can be checked here.
EXPANDED: Mapping[tuple[str, str], tuple[str, ...]] = {
    ("watchdog.py", "pyracast_watchdog_heartbeat"): ("supervise", "recover", "check_runtime"),
    ("watchdog.py", "pyracast_delivery_unhealthy"): ("failing_delivery",),
    ("rehearsal.py", "pyracast_rehearsal_passed"): ("rehearse",),
}
# Only operations_log.py names the log and its endpoint; no other script or template writes it.
ENDPOINT = re.compile(r"pyracast-operations|entries:write|logging\.googleapis\.com")
LOG_WRITE = re.compile(
    rf"{ENDPOINT.pattern}|gcloud logging write|operations_events\.py|operations_log\.report"
)
PROJECT = "project"
# The policies that existed before each alerted event had its own, with the event each matches.
EXISTING_POLICIES = {
    "backup": "pyracast_backup_failed",
    "runtime": "pyracast_runtime_unhealthy",
    "host": "pyracast_host_capacity",
    "certificate": "pyracast_certificate_expiring",
    "secrets": "pyracast_secrets_failed",
    "origin_pull": "pyracast_origin_pull_unenforced",
}


class Write:
    def __init__(self, severity: str, fields: frozenset[str]) -> None:
        self.severity = severity
        self.fields = fields


def reports(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Name) and node.func.id == "report")
        or (isinstance(node.func, ast.Attribute) and node.func.attr == "report")
    )


def returned_shapes(tree: ast.AST, functions: Iterable[str]) -> list[frozenset[str]]:
    shapes: list[frozenset[str]] = []
    for name in functions:
        (function,) = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        ]
        for node in ast.walk(function):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                keys = node.value.keys
                assert all(isinstance(key, ast.Constant) for key in keys), name
                shapes.append(frozenset(str(cast(ast.Constant, key).value) for key in keys))
    assert shapes, functions
    return shapes


def writes(
    root: Path = DEPLOY, expanded: Mapping[tuple[str, str], tuple[str, ...]] = EXPANDED
) -> dict[str, list[Write]]:
    found: dict[str, list[Write]] = defaultdict(list)
    test_alerts = 0
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not reports(node):
                continue
            assert isinstance(node, ast.Call)
            event, severity, *_ = node.args
            # The test alert writes the event its operator names, with that event's sample.
            if path.name == "operations_events.py" and ast.unparse(event) == "options.test_alert":
                test_alerts += 1
                continue
            assert isinstance(event, ast.Constant), path
            assert isinstance(severity, ast.Constant), path
            name = str(event.value)
            named = frozenset(str(item.arg) for item in node.keywords if item.arg)
            shapes = [named]
            if any(item.arg is None for item in node.keywords):
                functions = expanded.get((path.name, name))
                assert functions, f"{path.name}: {name} passes a mapping nobody checks"
                shapes = [named | shape for shape in returned_shapes(tree, functions)]
            found[name] += [Write(str(severity.value), shape) for shape in shapes]
    assert test_alerts == (1 if (root / "operations_events.py").exists() else 0)
    return found


def indirect_reports(root: Path = DEPLOY) -> list[str]:
    """Every way a host script reaches the report function other than calling it by name."""
    found: list[str] = []
    for path in root.glob("*.py"):
        if path.name == "operations_log.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        imported = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("operations_log"):
                imported |= any(alias.name == "report" for alias in node.names)
            if isinstance(node, ast.Import | ast.ImportFrom):
                found += [
                    f"{path.name}: {alias.name} as {alias.asname}"
                    for alias in node.names
                    if alias.asname
                    and alias.name.rsplit(".", 1)[-1] in ("operations_log", "report")
                ]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Name | ast.Attribute) or id(node) in called:
                continue
            if (isinstance(node, ast.Name) and node.id == "report" and imported) or (
                isinstance(node, ast.Attribute)
                and node.attr == "report"
                and ast.unparse(node.value).endswith("operations_log")
            ):
                found.append(f"{path.name}:{node.lineno} uses report without calling it")
    return found


def other_log_writers(paths: Iterable[Path]) -> list[str]:
    found: list[str] = []
    for path in paths:
        if path.name == "operations_log.py":
            continue
        content = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            # A Python helper writes through operations_log.report, which the call scan checks.
            strings = [
                node.value
                for node in ast.walk(ast.parse(content))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            ]
            written = any(ENDPOINT.search(string) for string in strings)
        else:
            written = bool(LOG_WRITE.search(content))
        if written:
            found.append(path.name)
    return found


def monitoring() -> str:
    return MONITORING.read_text(encoding="utf-8")


def operations_alerts() -> dict[str, str]:
    block = re.search(r"^  operations_alerts = \{\n(.*?)\n  \}$", monitoring(), re.S | re.M)
    assert block
    return dict(
        re.findall(r'^\s+(\w+)\s*=\s*\{\s*event\s*=\s*"(pyracast_[a-z_]+)"', block[1], re.M)
    )


def log_policies() -> dict[str, list[str]]:
    """Each operations log-match policy's filter, rendered for the event it is keyed by."""
    policy = resources(monitoring(), "google_monitoring_alert_policy")["operations"]
    return {
        key: filter_terms(policy, {"var.project_id": PROJECT, "each.value.event": event})
        for key, event in operations_alerts().items()
    }


def rate_policies() -> dict[str, list[str]]:
    """The filter of the log-based metric behind each threshold policy that counts one."""
    source = monitoring()
    metrics = resources(source, "google_logging_metric")
    return {
        name: filter_of(metrics[metric], source, {"var.project_id": PROJECT})
        for name, policy in resources(source, "google_monitoring_alert_policy").items()
        if "condition_threshold" in policy
        for metric in re.findall(r"google_logging_metric\.(\w+)\.name", policy)
    }


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, bool], dict[str, Any]]:
    """The entry operations_log sends for an event: a real line, or its test alert."""
    bodies: list[dict[str, Any]] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        bodies.append(json.loads(cast(bytes, request.data)))
        return io.BytesIO(b"{}")

    monkeypatch.setattr(operations_log, "log_resource", lambda: (PROJECT, {"type": "gce_instance"}))
    monkeypatch.setattr(operations_log, "access_token", lambda: "token")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    def entry(name: str, test: bool) -> dict[str, Any]:
        event = EVENTS[name]
        if test:
            assert operations_events.main(["--test-alert", name]) == 0
        else:
            assert operations_log.report(name, event.severity, **event.sample)
        body = bodies.pop()
        (line,) = body["entries"]
        return {"logName": body["logName"], "resource": body["resource"], **line}

    return entry


def runbook_rows() -> list[list[str]]:
    section = re.search(
        r"^### Alert events\n(.*?)(?=^#)", RUNBOOK.read_text(encoding="utf-8"), re.S | re.M
    )
    assert section
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in section[1].splitlines()
        if line.startswith("| `pyracast_")
    ]


def test_every_event_a_host_script_writes_is_in_the_vocabulary_with_its_severity() -> None:
    written = writes()

    assert set(written) <= set(EVENTS)
    for event, calls in written.items():
        assert {call.severity for call in calls} == {EVENTS[event].severity}, event


def test_every_event_of_the_vocabulary_is_written_by_a_host_script() -> None:
    assert set(EVENTS) <= set(writes())


@pytest.mark.parametrize("name", sorted(EVENTS))
def test_each_event_pages_through_one_log_policy_or_one_rate_or_stays_silent_with_its_reason(
    name: str, written: Callable[[str, bool], dict[str, Any]]
) -> None:
    logs, rates = log_policies(), rate_policies()
    line = written(name, False)
    matched = [key for key, terms in logs.items() if matches(terms, line)]
    counted = [key for key, terms in rates.items() if matches(terms, line)]
    alerts = EVENTS[name].alerts

    assert logs
    assert rates
    if alerts == "match":
        assert (len(matched), counted) == (1, [])
    elif alerts == "rate":
        assert (matched, len(counted)) == ([], 1)
    else:
        assert isinstance(alerts, operations_events.Silent)
        assert alerts.reason
        assert (matched, counted) == ([], [])
    if EVENTS[name].severity == "ERROR":
        assert alerts == "match"


@pytest.mark.parametrize("name", sorted(EVENTS))
def test_a_test_alert_reaches_its_events_log_policy_and_never_counts_toward_a_rate(
    name: str, written: Callable[[str, bool], dict[str, Any]]
) -> None:
    logs, rates = log_policies(), rate_policies()
    line = written(name, True)

    assert line["jsonPayload"]["test"] is True
    assert logs
    assert len([key for key, terms in logs.items() if matches(terms, line)]) == (
        1 if EVENTS[name].alerts == "match" else 0
    )
    assert [key for key, terms in rates.items() if matches(terms, line)] == []


def test_each_alerted_event_has_its_own_policy_and_the_existing_keys_keep_their_events() -> None:
    alerts = operations_alerts()
    policy = " ".join(
        resources(monitoring(), "google_monitoring_alert_policy")["operations"].split()
    )

    assert {key: alerts.get(key) for key in EXISTING_POLICIES} == EXISTING_POLICIES
    assert sorted(alerts.values()) == sorted(
        name for name, event in EVENTS.items() if event.alerts == "match"
    )
    assert "for_each = local.operations_alerts" in policy
    assert 'notification_rate_limit { period = "3600s" }' in policy
    assert '"${each.value.event}: ${each.value.description} ' in policy
    assert "alert_policy_per_event" not in monitoring()
    assert "alert_policy_per_event" not in VARIABLES.read_text(encoding="utf-8")


def test_every_event_has_one_runbook_entry_saying_what_to_check_and_how_to_recover() -> None:
    rows = runbook_rows()

    assert sorted(row[0] for row in rows) == sorted(f"`{name}`" for name in EVENTS)
    assert all(len(row) == 4 and all(row) for row in rows)


def test_the_restart_row_says_the_rate_pages_test_lines_never_count_and_what_to_check() -> None:
    ((_, means, check, _),) = [
        row for row in runbook_rows() if row[0] == "`pyracast_runtime_restarted`"
    ]

    assert "three" in means
    assert "within an hour" in means
    assert "test line" in means
    assert "docker inspect" in check
    assert "memory" in check
    assert "release" in check


def test_no_event_field_identifies_a_customer_an_address_or_a_key() -> None:
    fields = {field for calls in writes().values() for call in calls for field in call.fields}
    fields |= {field for event in EVENTS.values() for field in event.sample}

    assert {"unit_state", "missing_queues", "recovery_seconds"} <= fields
    assert not [field for field in fields if IDENTIFYING.search(field)]


def test_no_host_script_reaches_the_report_function_by_another_name() -> None:
    assert indirect_reports() == []


def test_no_shell_script_template_or_other_helper_writes_the_operations_log() -> None:
    paths = [*DEPLOY.glob("*.py"), *DEPLOY.glob("*.sh"), *TEMPLATES.iterdir()]

    assert len(paths) > 20
    assert other_log_writers(paths) == []


def test_the_scan_sees_attribute_calls_aliases_mappings_and_other_writers(tmp_path: Path) -> None:
    (tmp_path / "attribute.py").write_text(
        "from deploy import operations_log\n"
        'operations_log.report("pyracast_seen", "ERROR", count=1)\n'
    )
    (tmp_path / "renamed.py").write_text("from deploy.operations_log import report as emit\n")
    (tmp_path / "module.py").write_text("from deploy import operations_log as log\n")
    (tmp_path / "assigned.py").write_text(
        "from deploy.operations_log import report\nemit = report\n"
    )
    (tmp_path / "mapped.py").write_text(
        'def shape():\n    return {"seen": 1}\nreport("pyracast_mapped", "INFO", **shape())\n'
    )
    (tmp_path / "direct.py").write_text(
        '# Not through pyracast-operations.\nURL = "https://logging.googleapis.com/v2"\n'
    )
    (tmp_path / "write.sh").write_text("curl https://logging.googleapis.com/v2/entries:write\n")
    (tmp_path / "copy.sh").write_text("cp /source/operations_log.py /fixture/deploy/\n")

    with pytest.raises(AssertionError, match="pyracast_mapped passes a mapping nobody checks"):
        writes(tmp_path)
    found = writes(tmp_path, {("mapped.py", "pyracast_mapped"): ("shape",)})
    assert {name: [call.fields for call in calls] for name, calls in found.items()} == {
        "pyracast_seen": [frozenset({"count"})],
        "pyracast_mapped": [frozenset({"seen"})],
    }
    assert sorted(indirect_reports(tmp_path)) == [
        "assigned.py:2 uses report without calling it",
        "module.py: operations_log as log",
        "renamed.py: report as emit",
    ]
    assert other_log_writers(sorted(tmp_path.iterdir())) == ["direct.py", "write.sh"]


@pytest.mark.parametrize("name", sorted(EVENTS))
def test_every_event_has_a_test_alert_that_writes_only_its_operations_line(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        assert request.full_url == "https://logging.googleapis.com/v2/entries:write"
        sent.append(json.loads(cast(bytes, request.data)))
        return io.BytesIO(b"{}")

    monkeypatch.setattr(operations_log, "log_resource", lambda: ("project", {"type": "gce"}))
    monkeypatch.setattr(operations_log, "access_token", lambda: "token")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    event = EVENTS[name]

    assert operations_events.main(["--test-alert", name]) == 0
    ((entry,),) = [body["entries"] for body in sent]
    assert entry == {
        "severity": event.severity,
        "jsonPayload": {"event": name, "test": True, **event.sample},
    }
    assert frozenset(event.sample) in [call.fields for call in writes()[name]]


def test_a_test_alert_that_cannot_be_written_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unavailable(_path: str) -> str:
        raise OSError("metadata unavailable")

    operations_log.log_resource.cache_clear()
    monkeypatch.setattr(operations_log, "metadata", unavailable)
    try:
        assert operations_events.main(["--test-alert", "pyracast_backup_failed"]) == 1
    finally:
        operations_log.log_resource.cache_clear()
    assert capsys.readouterr().err.splitlines()[-1] == '{"event":"pyracast_operations_log_failed"}'


def test_a_test_alert_names_an_event_of_the_vocabulary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as refused:
        operations_events.main(["--test-alert", "pyracast_unknown"])

    assert refused.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
