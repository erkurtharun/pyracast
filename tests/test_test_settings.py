import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from django.db import connections


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_test_sessions_do_not_wait_for_wal_flush() -> None:
    # CI's --non-durable server is off on its own; source "client" proves the session option.
    for alias in ("default", "auth_cache"):
        with connections[alias].cursor() as cursor:
            cursor.execute(
                "SELECT setting, source FROM pg_settings WHERE name = 'synchronous_commit'"
            )
            assert cursor.fetchone() == ("off", "client")


SETTINGS_PROBE = """
import json
from django.conf import settings
names = (
    "DEBUG", "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS", "PYRACAST_PUBLIC_ORIGIN",
    "PYRACAST_CUTOFF_LOCAL", "PYRACAST_TRUST_PROXY",
    "PYRACAST_TRUSTED_PROXY_CIDRS", "PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS",
    "SECURE_PROXY_SSL_HEADER", "ALLAUTH_TRUSTED_CLIENT_IP_HEADER", "SECRET_KEY_FALLBACKS",
    "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SESSION_COOKIE_NAME", "CSRF_COOKIE_NAME",
    "SOCIALACCOUNT_PROVIDERS", "MAILERS", "DEFAULT_FROM_EMAIL", "STATIC_ROOT", "LOGGING",
)
values = {name: repr(getattr(settings, name)) for name in names}
print(json.dumps(values))
"""

HOSTILE_ENVIRONMENT = {
    "PYRACAST_DEBUG": "1",
    "PYRACAST_CUTOFF_LOCAL": "03:00",
    "PYRACAST_TRUST_PROXY": "1",
    "PYRACAST_HOSTS": "example.org",
    "PYRACAST_CSRF_ORIGINS": "https://example.org",
    "PYRACAST_PUBLIC_ORIGIN": "https://example.org",
    "PYRACAST_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
    "PYRACAST_GOOGLE_CLIENT_ID": "local-client",
    "PYRACAST_GOOGLE_CLIENT_SECRET": "local-secret",
    "PYRACAST_FROM_EMAIL": "Local <local@example.org>",
    "PYRACAST_EMAIL_PORT": "2525",
    "PYRACAST_LOG_LEVEL": "DEBUG",
}


def settings_in(directory: Path, environment: dict[str, str]) -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    base = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("PYRACAST_", "DJANGO_"))
    }
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", SETTINGS_PROBE],
        cwd=directory,
        env=base
        | environment
        | {"DJANGO_SETTINGS_MODULE": "tests.settings", "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return cast("dict[str, str]", json.loads(result.stdout))


def test_test_settings_ignore_a_hostile_environment(tmp_path: Path) -> None:
    clean = tmp_path / "clean"
    hostile = tmp_path / "hostile"
    clean.mkdir()
    hostile.mkdir()
    (hostile / ".env").write_text(
        "PYRACAST_DEBUG=1\nPYRACAST_EMAIL_BACKEND=console\nPYRACAST_CUTOFF_LOCAL=05:00\n",
        encoding="utf-8",
    )

    expected = settings_in(clean, {})
    assert settings_in(hostile, HOSTILE_ENVIRONMENT) == expected
    assert expected["DEBUG"] == "False"
    assert expected["PYRACAST_CUTOFF_LOCAL"] == "datetime.time(16, 0)"
    assert expected["SESSION_COOKIE_NAME"] == "'sessionid'"
