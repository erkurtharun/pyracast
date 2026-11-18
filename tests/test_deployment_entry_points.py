import re
import runpy
import signal
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SCRIPTS = sorted(
    path.name
    for path in DEPLOY.glob("*.py")
    if 'if __name__ == "__main__":' in path.read_text(encoding="utf-8")
)
# These two read no options: one prompts for secrets, one is driven by the installer's arguments.
UNPARSED = {"create_secrets.py", "release_record.py"}


def run_script(
    name: str, arguments: list[str], monkeypatch: pytest.MonkeyPatch
) -> int | str | None:
    monkeypatch.setattr(sys, "argv", [str(DEPLOY / name), *arguments])
    monkeypatch.setattr(sys, "path", [*sys.path])
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(DEPLOY / name), run_name="__main__")
    code = stopped.value.code
    assert code is None or isinstance(code, int | str)
    return code


def test_every_host_script_is_listed_by_its_entry_point() -> None:
    assert set(SCRIPTS) > UNPARSED
    assert {"backup.py", "watchdog.py", "release_state.py", "release_unit.py"} <= set(SCRIPTS)


@pytest.mark.parametrize("name", sorted(set(SCRIPTS) - UNPARSED))
def test_every_host_script_explains_its_usage_when_run_directly(
    name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_script(name, ["--help"], monkeypatch) == 0
    assert capsys.readouterr().out.startswith("usage:")


def test_the_recovery_reader_refuses_a_command_line_without_its_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_script("release_record.py", [], monkeypatch) == 2


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        (
            "bootstrap_database.py",
            ["converge", "missing-release", "missing-secrets"],
            "Database provisioning failed; inspect database state and secret configuration.",
        ),
        (
            "runtime_secrets.py",
            ["missing-host.json"],
            "Secret preparation failed; check configuration, IAM and secret format.",
        ),
    ],
)
def test_a_failing_provisioning_script_exits_with_a_redacted_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    arguments: list[str],
    message: str,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert run_script(name, arguments, monkeypatch) == message


def test_secret_creation_runs_as_a_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    replies = iter(["", ""])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(replies))
    monkeypatch.setattr(sys, "argv", [str(DEPLOY / "create_secrets.py")])

    runpy.run_path(str(DEPLOY / "create_secrets.py"), run_name="__main__")

    assert sorted(path.name for path in (tmp_path / "secrets").iterdir()) == [
        "database.json",
        "migration.json",
        "runtime.json",
    ]
    assert re.search(r"authorize_gmail\.py", capsys.readouterr().out)
