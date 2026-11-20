import argparse
import base64
import http.client
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import host_contract
from deploy.host_contract import HostConfigError
from deploy.operations_log import report

# Each release reads its own copy, so writing a new release's secrets never changes a running one.
SECRETS = Path("/run/pyracast/releases")
# pyracast.service gives its whole start 300 s and Compose's wait 180 s of them.
RETRY_SECONDS = 60
METADATA_TIMEOUT_SECONDS = 10
SECRET_TIMEOUT_SECONDS = 15
MESSAGES = {
    "configuration": "{detail}",
    "identity": "the metadata server gave no access token for the VM's service account.",
    "permission": (
        "Secret Manager refused the {secret} secret to the VM's service account; check its "
        "secretAccessor binding."
    ),
    "version": "the pinned {secret} secret version does not exist or is disabled.",
    "unavailable": "{detail} within {seconds} s.",
    "format": "the {secret} secret is not a JSON object of environment strings with its keys.",
    "write": "the environment files of the release could not be written ({detail}).",
    "unexpected": "{detail} while preparing the {secret} secret.",
}
Files = dict[str, dict[str, str]]


class SecretPreparationError(Exception):
    def __init__(self, category: str, secret: str, detail: str = "") -> None:
        super().__init__(category)
        self.category = category
        self.secret = secret
        self.detail = detail

    def __str__(self) -> str:
        message = MESSAGES[self.category].format(
            secret=self.secret, detail=self.detail, seconds=RETRY_SECONDS
        )
        return f"Secret preparation failed ({self.category}): {message}"


class Deadline:
    def __init__(self, expires: float) -> None:
        self.expires = expires

    def timeout(self, limit: float) -> float:
        return max(1.0, min(limit, self.expires - time.monotonic()))

    def wait(self, seconds: float) -> bool:
        if time.monotonic() + seconds >= self.expires:
            return False
        time.sleep(seconds)
        return True


def environment_values(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Secret must be a nonempty JSON object of environment strings.")
    values: dict[str, str] = {}
    for key, value in payload.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
            or not isinstance(value, str)
            or any(character in value for character in "\r\n\x00")
        ):
            raise ValueError("Secret contains an invalid environment entry.")
        values[key] = value
    return values


def runtime_files(values: Mapping[str, str]) -> Files:
    if any(not values.get(key) for key in host_contract.RUNTIME_REQUIRED):
        raise ValueError("Runtime secret is missing required application, database or broker keys.")
    broker_password = values["RABBITMQ_DEFAULT_PASS"]
    application = {
        key: value
        for key, value in values.items()
        if key.startswith(host_contract.APPLICATION_PREFIXES)
    }
    application["PYRACAST_RABBITMQ_URL"] = (
        f"amqp://{host_contract.BROKER_USER}:{quote(broker_password, safe='')}@rabbitmq:5672/%2F"
    )
    broker = {
        "RABBITMQ_DEFAULT_USER": host_contract.BROKER_USER,
        "RABBITMQ_DEFAULT_PASS": broker_password,
    }
    return {"runtime.env": application, "broker.env": broker}


def database_files(payload: object) -> Files:
    values = environment_values(payload)
    if set(values) != host_contract.DATABASE_KEYS or not values["POSTGRES_PASSWORD"]:
        raise ValueError("Database secret must contain only POSTGRES_PASSWORD.")
    return {"database.env": values}


def migration_files(payload: object) -> Files:
    values = environment_values(payload)
    if set(values) != host_contract.MIGRATION_KEYS or not all(values.values()):
        raise ValueError("Migration secret must contain only PGUSER and PGPASSWORD.")
    return {"migration.env": values}


def write_environment(path: Path, values: Mapping[str, str]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("".join(f"{key}={value}\n" for key, value in sorted(values.items())))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_files(directory: Path, files: Files) -> None:
    for name, values in files.items():
        write_environment(directory / name, values)


def fetch_json(
    request: urllib.request.Request,
    limit: float,
    deadline: Deadline,
    failure: SecretPreparationError,
) -> Any:
    delay = 1.0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=deadline.timeout(limit)) as response:  # noqa: S310
                return json.load(response)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code != 429 and error.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):
            pass
        if not deadline.wait(delay):
            raise failure
        delay = min(2 * delay, 8.0)


def instance_token(deadline: Deadline) -> str:
    # The link-local metadata endpoint is the VM's attached identity, never user input.
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    unavailable = SecretPreparationError(
        "unavailable", "host", "the metadata server did not answer"
    )
    try:
        token = fetch_json(request, METADATA_TIMEOUT_SECONDS, deadline, unavailable)["access_token"]
    except (urllib.error.HTTPError, ValueError, KeyError, TypeError):
        raise SecretPreparationError("identity", "host") from None
    if not isinstance(token, str) or not token:
        raise SecretPreparationError("identity", "host")
    return token


def access_secret(resource: str, token: str, secret: str, deadline: Deadline) -> object:
    request = urllib.request.Request(
        f"https://secretmanager.googleapis.com/v1/{resource}:access",
        headers={"Authorization": f"Bearer {token}"},
    )
    unavailable = SecretPreparationError(
        "unavailable", secret, f"Secret Manager did not answer for the {secret} secret"
    )
    try:
        payload = fetch_json(request, SECRET_TIMEOUT_SECONDS, deadline, unavailable)
    except urllib.error.HTTPError as error:
        category = "permission" if error.code in (401, 403) else "version"
        raise SecretPreparationError(category, secret) from None
    except ValueError:
        raise SecretPreparationError("format", secret) from None
    try:
        data = base64.b64decode(payload["payload"]["data"], validate=True)
        return json.loads(data)
    except (KeyError, TypeError, ValueError):
        raise SecretPreparationError("format", secret) from None


def staged_directory(script: Path) -> Path:
    return SECRETS / script.resolve().parents[1].name


def migration_secret(path: Path) -> Files:
    try:
        return migration_files(json.loads(path.read_text(encoding="utf-8")))
    except OSError:
        raise SecretPreparationError(
            "configuration", "migration", "the migration secret file cannot be read."
        ) from None
    except ValueError:
        raise SecretPreparationError("format", "migration") from None


def host_secrets(path: Path) -> Files:
    try:
        config = host_contract.load(path)
    except HostConfigError as error:
        raise SecretPreparationError("configuration", "host", str(error)) from None
    deadline = Deadline(time.monotonic() + RETRY_SECONDS)
    token = instance_token(deadline)
    runtime = access_secret(config.runtime_secret_version, token, "runtime", deadline)
    try:
        files = runtime_files(environment_values(runtime))
    except ValueError:
        raise SecretPreparationError("format", "runtime") from None
    database = access_secret(config.database_secret_version, token, "database", deadline)
    try:
        return files | database_files(database)
    except ValueError:
        raise SecretPreparationError("format", "database") from None


def prepare(arguments: argparse.Namespace, directory: Path) -> None:
    if arguments.migration is not None:
        files, secret = migration_secret(arguments.migration), "migration"
    else:
        files, secret = host_secrets(arguments.host_config), "runtime"
    try:
        write_files(directory, files)
    except OSError as error:
        raise SecretPreparationError(
            "write", secret, error.strerror or type(error).__name__
        ) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("host_config", type=Path)
    parser.add_argument("--migration", type=Path)
    arguments = parser.parse_args(argv)
    secret = "migration" if arguments.migration is not None else "host"
    try:
        prepare(arguments, staged_directory(Path(__file__)))
    except SecretPreparationError as failure:
        print(failure, file=sys.stderr)
        report("pyracast_secrets_failed", "ERROR", category=failure.category, secret=failure.secret)
        return 1
    except Exception as error:
        print(SecretPreparationError("unexpected", secret, type(error).__name__), file=sys.stderr)
        report("pyracast_secrets_failed", "ERROR", category="unexpected", secret=secret)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
