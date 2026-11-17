import json
import os
import shutil
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import override_settings

from scripts import generate_api_schema


@pytest.mark.parametrize("debug", ["0", "1"])
def test_schema_generation_ignores_local_runtime_configuration(tmp_path: Path, debug: str) -> None:
    root = Path(__file__).resolve().parents[1]
    (tmp_path / ".env").write_text(
        "PYRACAST_DEBUG=1\nPYRACAST_EMAIL_BACKEND=console\nPYRACAST_CUTOFF_LOCAL=invalid\n",
        encoding="utf-8",
    )
    output = tmp_path / "schema.json"
    subprocess.run(  # noqa: S603
        [sys.executable, str(root / "scripts/generate_api_schema.py"), "--output", str(output)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYRACAST_DEBUG": debug,
            "PYRACAST_EMAIL_BACKEND": "console",
            "PYRACAST_SECRET_KEY": "",
            "PYRACAST_EMAIL_PORT": "invalid",
            "PGPORT": "invalid",
            "DJANGO_SETTINGS_MODULE": "unavailable.settings",
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    generated = json.loads(output.read_text(encoding="utf-8"))
    committed = json.loads((root / "web/openapi.json").read_text(encoding="utf-8"))
    assert generated == committed
    assert generated["components"]["securitySchemes"]["cookieAuth"]["name"] == (
        "__Host-pyracast-sessionid"
    )


@pytest.mark.parametrize("cookie_name", ["sessionid", "__Host-pyracast-sessionid"])
def test_runtime_schema_uses_its_actual_session_cookie(cookie_name: str) -> None:
    output = StringIO()
    with override_settings(SESSION_COOKIE_NAME=cookie_name):
        call_command(
            "spectacular", format="openapi-json", validate=True, fail_on_warn=True, stdout=output
        )

    schema = json.loads(output.getvalue())
    assert schema["components"]["securitySchemes"]["cookieAuth"]["name"] == cookie_name


def test_check_mode_reports_a_stale_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "web/openapi.json").read_text(encoding="utf-8"))
    schema["info"]["title"] = "Stale"
    (tmp_path / "openapi.json").write_text(json.dumps(schema), encoding="utf-8")
    monkeypatch.setattr(generate_api_schema, "WEB", tmp_path)

    assert generate_api_schema.main(["--check"]) == 1

    output = capsys.readouterr().out
    assert "web/openapi.json is stale" in output
    assert "npm --prefix web run api:generate" in output


COMMITTED_SCHEMA = Path(__file__).resolve().parents[1] / "web/openapi.json"


def committed_web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, types: bytes) -> Path:
    (tmp_path / "src/lib").mkdir(parents=True)
    (tmp_path / "openapi.json").write_bytes(COMMITTED_SCHEMA.read_bytes())
    (tmp_path / "src/lib/schema.d.ts").write_bytes(types)
    monkeypatch.setattr(generate_api_schema, "WEB", tmp_path)

    def fresh_schema(output: Path) -> int:
        output.write_bytes(COMMITTED_SCHEMA.read_bytes())
        return 0

    monkeypatch.setattr(generate_api_schema, "generate_openapi", fresh_schema)
    return tmp_path


def node_tools(monkeypatch: pytest.MonkeyPatch, types: bytes) -> None:
    def web_tool(*arguments: str, stdin: bytes | None = None) -> bytes:
        if arguments[0] == "openapi-typescript":
            return types
        assert arguments[0] == "prettier"
        assert stdin is not None
        return stdin

    monkeypatch.setattr(generate_api_schema, "web_tool", web_tool)


@pytest.mark.parametrize(
    ("committed_types", "stale"), [(b"fresh types\n", False), (b"stale types\n", True)]
)
def test_check_mode_compares_the_generated_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    committed_types: bytes,
    stale: bool,
) -> None:
    web = committed_web(tmp_path, monkeypatch, committed_types)
    node_tools(monkeypatch, b"fresh types\n")

    assert generate_api_schema.main(["--check"]) == int(stale)

    output = capsys.readouterr().out
    assert ("web/src/lib/schema.d.ts is stale" in output) is stale
    assert "web/openapi.json is stale" not in output
    assert ("API schema and generated types are current." in output) is not stale
    assert (web / "src/lib/schema.d.ts").read_bytes() == committed_types


def test_check_mode_reports_a_schema_that_differs_only_in_formatting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    web = committed_web(tmp_path, monkeypatch, b"fresh types\n")
    compact = json.dumps(json.loads(COMMITTED_SCHEMA.read_bytes())).encode()
    (web / "openapi.json").write_bytes(compact)
    node_tools(monkeypatch, b"fresh types\n")

    assert generate_api_schema.main(["--check"]) == 1

    output = capsys.readouterr().out
    assert "web/openapi.json is stale" in output
    assert "schema.d.ts is stale" not in output
    assert (web / "openapi.json").read_bytes() == compact


def test_check_mode_requires_npx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    committed_web(tmp_path, monkeypatch, b"fresh types\n")
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert generate_api_schema.main(["--check"]) == 1

    assert "npx is missing" in capsys.readouterr().err


def test_check_mode_returns_the_exit_code_of_a_failing_node_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    committed_web(tmp_path, monkeypatch, b"fresh types\n")

    def failing_tool(*arguments: str, stdin: bytes | None = None) -> bytes:
        raise subprocess.CalledProcessError(4, ["npx", *arguments], b"", b"prettier failed\n")

    monkeypatch.setattr(generate_api_schema, "web_tool", failing_tool)

    assert generate_api_schema.main(["--check"]) == 4

    captured = capsys.readouterr()
    assert "prettier failed" in captured.err
    assert "current" not in captured.out
