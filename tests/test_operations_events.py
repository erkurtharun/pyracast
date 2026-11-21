import ast
import io
import json
import re
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

import pytest

from deploy import operations_events, operations_log

EVENTS = operations_events.EVENTS
ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
MONITORING = ROOT / "infra" / "terraform" / "monitoring.tf"
RUNBOOK = ROOT / "docs" / "DEPLOYMENT.md"
# An operations line carries ids, states and counts, never who or what a customer is.
IDENTIFYING = re.compile(r"mail|address|name|key|domain|site|host|url|message|stderr|stdout")


class Write:
    def __init__(self, severity: str, fields: frozenset[str], expanded: bool) -> None:
        self.severity = severity
        self.fields = fields
        self.expanded = expanded


def writes() -> dict[str, list[Write]]:
    found: dict[str, list[Write]] = defaultdict(list)
    for path in DEPLOY.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "report"
            ):
                continue
            event, severity, *_ = node.args
            assert isinstance(event, ast.Constant), path
            assert isinstance(severity, ast.Constant), path
            found[str(event.value)].append(
                Write(
                    str(severity.value),
                    frozenset(str(item.arg) for item in node.keywords if item.arg),
                    any(item.arg is None for item in node.keywords),
                )
            )
    return found


def alert_policies() -> list[str]:
    block = re.search(
        r'resource "google_monitoring_alert_policy" "operations" \{\n  for_each = \{\n(.*?)\n  \}',
        MONITORING.read_text(encoding="utf-8"),
        re.S,
    )
    assert block
    return re.findall(r'event\s*=\s*"(pyracast_[a-z_]+)"', block[1])


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


def test_every_alerted_event_has_one_alert_policy_and_every_error_is_alerted() -> None:
    policies = alert_policies()
    alerted = {name for name, event in EVENTS.items() if event.alerts}

    assert sorted(policies) == sorted(alerted)
    assert {name for name, event in EVENTS.items() if event.severity == "ERROR"} <= alerted


def test_every_event_has_one_runbook_entry_saying_what_to_check_and_how_to_recover() -> None:
    rows = runbook_rows()

    assert sorted(row[0] for row in rows) == sorted(f"`{name}`" for name in EVENTS)
    assert all(len(row) == 4 and all(row) for row in rows)


def test_no_event_field_identifies_a_customer_an_address_or_a_key() -> None:
    fields = {field for calls in writes().values() for call in calls for field in call.fields}
    fields |= {field for event in EVENTS.values() for field in event.sample}

    assert fields
    assert not [field for field in fields if IDENTIFYING.search(field)]


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
    calls = writes()[name]
    if not any(call.expanded for call in calls):
        assert set(event.sample) in [set(call.fields) for call in calls]


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
