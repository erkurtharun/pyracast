import os
from collections.abc import Callable
from pathlib import Path
from runpy import run_path
from typing import Any
from unittest import mock

import pytest
import yaml
from django.core.checks import WARNING, run_checks
from django.test import override_settings

from pyracast import settings as project_settings
from tests.support.runtime_environment import RUNTIME_ENV

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "pyracast.example"
# The installer runs `check --deploy` in a container of the shipped image; these are the settings
# it builds from the Compose environment and the runtime secret. The test keeps its own database,
# caches, applications and middleware.
KEPT = frozenset(
    {
        "DATABASES",
        "CACHES",
        "LOGGING",
        "INSTALLED_APPS",
        "MIDDLEWARE",
        "TEMPLATES",
        "PASSWORD_HASHERS",
    }
)


def compose_environment() -> dict[str, str]:
    compose = yaml.safe_load((ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8"))
    environment: dict[str, str] = compose["services"]["migrate"]["environment"]
    assert environment == compose["services"]["api"]["environment"]
    return {name: value.replace("${SITE_ADDRESS}", DOMAIN) for name, value in environment.items()}


def runtime_environment(**changes: str | None) -> dict[str, str]:
    values = {**RUNTIME_ENV, **changes}
    return {name: value for name, value in values.items() if value is not None}


def deployment_messages(
    tmp_path: Path, runtime: dict[str, str], compose: dict[str, str]
) -> list[tuple[str, int]]:
    # Compose gives `environment` precedence over `env_file`.
    with mock.patch.dict(os.environ, {**runtime, **compose}, clear=True):
        configuration = run_path(str(Path(project_settings.__file__)))
    production = {
        name: value for name, value in configuration.items() if name.isupper() and name not in KEPT
    }
    with override_settings(**production):
        return [
            (str(message.id), message.level)
            for message in run_checks(include_deployment_checks=True)
            if message.level >= WARNING
        ]


@pytest.fixture
def deployed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., list[tuple[str, int]]]:
    monkeypatch.chdir(tmp_path)

    def check(runtime: dict[str, Any] | None = None, **compose: str) -> list[tuple[str, int]]:
        return deployment_messages(
            tmp_path,
            runtime_environment(**(runtime or {})),
            {**compose_environment(), **compose},
        )

    return check


def test_the_shipped_compose_file_and_runtime_secret_pass_every_deployment_check(
    deployed: Callable[..., list[tuple[str, int]]],
) -> None:
    assert deployed() == []


@pytest.mark.parametrize(
    ("compose", "refused"),
    [
        pytest.param({"PYRACAST_TRUST_PROXY": "0"}, "accounts.E004", id="proxy-trust-off"),
        pytest.param({"PYRACAST_TRUSTED_PROXY_CIDRS": ""}, "accounts.E004", id="no-gateway"),
        pytest.param(
            {"PYRACAST_TRUSTED_PROXY_CIDRS": "gateway"}, "accounts.E004", id="invalid-gateway"
        ),
        pytest.param({"PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS": ""}, "accounts.E004", id="no-edge"),
        pytest.param(
            {"PYRACAST_PUBLIC_ORIGIN": f"http://{DOMAIN}"}, "accounts.E002", id="http-origin"
        ),
        pytest.param(
            {"PYRACAST_CSRF_ORIGINS": "https://other.example"}, "accounts.E005", id="csrf-origin"
        ),
        pytest.param({"PYRACAST_CSRF_ORIGINS": ""}, "accounts.E005", id="no-csrf-origin"),
        pytest.param(
            {"PYRACAST_HOSTS": "localhost,127.0.0.1"}, "accounts.E005", id="host-not-allowed"
        ),
    ],
)
def test_each_deployment_check_refuses_its_setting_changed_in_the_compose_file(
    deployed: Callable[..., list[tuple[str, int]]], compose: dict[str, str], refused: str
) -> None:
    assert [identifier for identifier, _ in deployed(**compose)] == [refused]


def test_the_broker_check_refuses_a_runtime_environment_without_the_broker_url(
    deployed: Callable[..., list[tuple[str, int]]],
) -> None:
    assert deployed({"PYRACAST_RABBITMQ_URL": None}) == [("background.E001", 40)]


def test_the_mail_check_refuses_a_runtime_secret_without_a_public_relay(
    deployed: Callable[..., list[tuple[str, int]]],
) -> None:
    assert deployed({"PYRACAST_EMAIL_HOST": None}) == [("accounts.E001", 40)]
