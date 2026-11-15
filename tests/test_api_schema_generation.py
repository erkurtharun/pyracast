import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import override_settings


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
