import dataclasses
import json
import re
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any

CERTIFICATE = r"-----BEGIN CERTIFICATE-----\n[A-Za-z0-9+/=\n]+-----END CERTIFICATE-----\n"
CERTIFICATES = re.compile(rf"(?:{CERTIFICATE}\n?)+")
MANAGED_NAME = r"projects/[a-z0-9-]+/secrets/[a-zA-Z0-9_-]+"
MANAGED_VERSION = rf"{MANAGED_NAME}/versions/[0-9]+"
# Release values go into release.env, which Compose reads literally; each pattern stays inside
# release_environment.VALUE.
DOMAIN = r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}"
EMAIL = r"[a-zA-Z0-9.:/_+%-]+@[a-zA-Z0-9:/_+%-]+(?:\.[a-zA-Z0-9:/_+%-]+)+"


class HostConfigError(ValueError):
    pass


def _loads_as_x509(certificate: str) -> bool:
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=certificate)
    except ssl.SSLError:
        return False
    return True


def _matching(pattern: str) -> Callable[[object], str | None]:
    compiled = re.compile(pattern)
    return lambda value: value if isinstance(value, str) and compiled.fullmatch(value) else None


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _authority(value: object) -> str | None:
    if value == "":
        return ""
    if not isinstance(value, str):
        return None
    authority = value.strip() + "\n"
    if CERTIFICATES.fullmatch(authority) is None:
        return None
    if not all(map(_loads_as_x509, re.findall(CERTIFICATE, authority))):
        return None
    return authority


@dataclasses.dataclass(frozen=True)
class Setting:
    key: str
    expectation: str
    parse: Callable[[object], str | bool | None]
    default: str | bool | None = None

    @property
    def required(self) -> bool:
        return self.default is None


# /etc/pyracast/host.json, which the Terraform boot script writes. Readers outside load() read only
# these keys and an optional one with its default.
SETTINGS = (
    Setting("project_id", "a Google Cloud project ID", _matching(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")),
    Setting("runtime_secret_id", "a Secret Manager secret name", _matching(MANAGED_NAME)),
    Setting(
        "runtime_secret_version",
        "a numeric version of runtime_secret_id",
        _matching(MANAGED_VERSION),
    ),
    Setting(
        "database_secret_version", "a numeric Secret Manager version", _matching(MANAGED_VERSION)
    ),
    Setting(
        "backup_bucket",
        "a Cloud Storage bucket name",
        _matching(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]"),
    ),
    Setting("domain", "a lowercase DNS hostname", _matching(DOMAIN)),
    Setting("acme_email", "an email address", _matching(EMAIL)),
    Setting(
        "origin_pull_ca",
        "PEM X.509 certificates of the edge's authority",
        _authority,
        default="",
    ),
    Setting("daily_backups", "a JSON boolean", _flag, default=False),
)
BY_KEY = {setting.key: setting for setting in SETTINGS}
KEYS = frozenset(BY_KEY)


@dataclasses.dataclass(frozen=True)
class HostConfig:
    project_id: str
    runtime_secret_id: str
    runtime_secret_version: str
    database_secret_version: str
    backup_bucket: str
    domain: str
    acme_email: str
    origin_pull_ca: str
    daily_backups: bool


def _refusal(setting: Setting) -> str:
    if setting.key == "origin_pull_ca":
        return f"Host setting {setting.key} must hold only {setting.expectation}."
    return f"Host setting {setting.key} must be {setting.expectation}."


def parse(values: object) -> HostConfig:
    if not isinstance(values, dict):
        raise HostConfigError("Host configuration must be a JSON object of settings.")
    unknown = sorted(set(values) - KEYS)
    if unknown:
        name = unknown[0] if unknown[0].isprintable() else repr(unknown[0])
        raise HostConfigError(
            f"Host setting {name} is unknown; remove it or add it to deploy/host_contract.py."
        )
    parsed: dict[str, Any] = {}
    for setting in SETTINGS:
        if setting.key in values:
            value = values[setting.key]
        elif setting.default is not None:
            value = setting.default
        else:
            raise HostConfigError(f"Host setting {setting.key} is missing; it is required.")
        result = setting.parse(value)
        if result is None:
            raise HostConfigError(_refusal(setting))
        parsed[setting.key] = result
    if not parsed["runtime_secret_version"].startswith(parsed["runtime_secret_id"] + "/versions/"):
        raise HostConfigError(_refusal(BY_KEY["runtime_secret_version"]))
    return HostConfig(**parsed)


def load(path: Path) -> HostConfig:
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise HostConfigError(
            f"Host configuration {path} cannot be read ({error.strerror})."
        ) from None
    except ValueError:
        raise HostConfigError(f"Host configuration {path} is not valid JSON.") from None
    return parse(values)


# deploy/compose.yaml reads these from release.env, in the order release_environment writes them.
IMAGE_KEYS = ("BACKEND_IMAGE", "WEB_IMAGE", "GATEWAY_IMAGE", "POSTGRES_IMAGE", "RABBITMQ_IMAGE")
SECRET_FILES = {
    "RUNTIME_ENV_FILE": "runtime.env",
    "BROKER_ENV_FILE": "broker.env",
    "DATABASE_ENV_FILE": "database.env",
    "MIGRATION_ENV_FILE": "migration.env",
}
RELEASE_KEYS = (*IMAGE_KEYS, "SITE_ADDRESS", "ACME_EMAIL", *SECRET_FILES, "ORIGIN_PULL")
ORIGIN_PULL_CA_KEY = "ORIGIN_PULL_CA"
# Defaults in deploy/compose.yaml that only the smoke changes.
COMPOSE_DEFAULTS = ("HTTP_BIND", "HTTPS_BIND", "LOCAL_HTTPS_BIND", "DATA_ROOT")

RUNTIME_ROLE = "pyracast_app"
OWNER_ROLE = "pyracast_owner"
BROKER_USER = "pyracast"
APPLICATION_PREFIXES = ("PYRACAST_", "PG", "EPIAS_")
RUNTIME_REQUIRED = frozenset(
    {"PYRACAST_SECRET_KEY", "PGUSER", "PGPASSWORD", "RABBITMQ_DEFAULT_PASS"}
)
MIGRATION_KEYS = frozenset({"PGUSER", "PGPASSWORD"})
DATABASE_KEYS = frozenset({"POSTGRES_PASSWORD"})
# Settings the application reads that the runtime secret carries; Compose sets the others.
RUNTIME_SETTINGS = frozenset(
    {
        "EPIAS_PASSWORD",
        "EPIAS_USERNAME",
        "PGPASSWORD",
        "PGUSER",
        "PYRACAST_CUTOFF_LOCAL",
        "PYRACAST_EMAIL_BACKEND",
        "PYRACAST_EMAIL_HOST",
        "PYRACAST_EMAIL_PASSWORD",
        "PYRACAST_EMAIL_PORT",
        "PYRACAST_EMAIL_SSL",
        "PYRACAST_EMAIL_TLS",
        "PYRACAST_EMAIL_USER",
        "PYRACAST_FROM_EMAIL",
        "PYRACAST_GMAIL_CLIENT_ID",
        "PYRACAST_GMAIL_CLIENT_SECRET",
        "PYRACAST_GMAIL_REFRESH_TOKEN",
        "PYRACAST_GOOGLE_CLIENT_ID",
        "PYRACAST_GOOGLE_CLIENT_SECRET",
        "PYRACAST_LOG_LEVEL",
        "PYRACAST_MICROSOFT_CLIENT_ID",
        "PYRACAST_MICROSOFT_CLIENT_SECRET",
        "PYRACAST_OPEN_METEO_API_KEY",
        "PYRACAST_SECRET_KEY",
        "PYRACAST_SECRET_KEY_FALLBACKS",
        "PYRACAST_STATIC_ROOT",
        "PYRACAST_WEATHER_ENABLED",
        "RABBITMQ_DEFAULT_PASS",
    }
)
