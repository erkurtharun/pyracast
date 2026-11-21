import ast
import io
import json
import re
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from deploy import operations_events, operations_log

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
FAMILIES = {"backup", "runtime", "host", "certificate", "secrets", "origin_pull"}


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


def operations_alerts() -> dict[str, tuple[str, str]]:
    block = re.search(
        r"^  operations_alerts = \{\n(.*?)\n  \}$",
        MONITORING.read_text(encoding="utf-8"),
        re.S | re.M,
    )
    assert block
    return {
        key: (family, event)
        for key, family, event in re.findall(
            r'^\s+(\w+)\s*=\s*\{\s*family\s*=\s*"(\w+)",\s*event\s*=\s*"(pyracast_[a-z_]+)"',
            block[1],
            re.M,
        )
    }


def alert_policies(per_event: bool) -> dict[str, list[str]]:
    """The events each operations policy matches, as monitoring.tf's locals build them."""
    alerts = operations_alerts()
    if per_event:
        return {key: [event] for key, (_, event) in alerts.items()}
    families: dict[str, list[str]] = defaultdict(list)
    for family, event in alerts.values():
        families[family].append(event)
    return dict(families)


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


@pytest.mark.parametrize("per_event", [False, True], ids=["by_family", "per_event"])
def test_every_alerted_event_is_matched_by_exactly_one_policy_in_either_mode(
    per_event: bool,
) -> None:
    matched = Counter(event for events in alert_policies(per_event).values() for event in events)
    alerted = {name for name, event in EVENTS.items() if event.alerts}

    assert set(matched) == alerted
    assert set(matched.values()) == {1}
    assert {name for name, event in EVENTS.items() if event.severity == "ERROR"} <= alerted


def test_the_default_alerts_each_family_through_the_policies_that_existed_before() -> None:
    by_family = alert_policies(per_event=False)
    per_event = alert_policies(per_event=True)
    monitoring = MONITORING.read_text(encoding="utf-8")

    assert set(by_family) == FAMILIES
    assert set(per_event) > FAMILIES
    for family in FAMILIES:
        assert per_event[family][0] in by_family[family]
    assert re.search(
        r'variable "alert_policy_per_event" \{\n.*?type\s*=\s*bool\n\s*default\s*=\s*false\n',
        VARIABLES.read_text(encoding="utf-8"),
        re.S,
    )
    assert "operations_policies = var.alert_policy_per_event ?" in monitoring
    policy = re.search(
        r'resource "google_monitoring_alert_policy" "operations" \{\n(.*?)\n\}', monitoring, re.S
    )
    assert policy
    assert "for_each = local.operations_policies" in " ".join(policy[1].split())
    assert r'"jsonPayload.event=\"${alert.event}\""' in policy[1]
    assert '"${alert.event}: ${alert.description}"' in policy[1]


def test_every_event_has_one_runbook_entry_saying_what_to_check_and_how_to_recover() -> None:
    rows = runbook_rows()

    assert sorted(row[0] for row in rows) == sorted(f"`{name}`" for name in EVENTS)
    assert all(len(row) == 4 and all(row) for row in rows)


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
