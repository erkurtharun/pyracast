import argparse
import base64
import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

# Each release reads its own copy, so writing a new release's secrets never changes a running one.
SECRETS = Path("/run/pyracast/releases")


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


def write_environment(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("".join(f"{key}={value}\n" for key, value in sorted(values.items())))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def access_secret(resource: str) -> dict[str, str]:
    if not re.fullmatch(r"projects/[a-z0-9-]+/secrets/[a-zA-Z0-9_-]+/versions/[0-9]+", resource):
        raise ValueError("Pin the runtime secret to a numeric Secret Manager version.")
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    # The link-local metadata endpoint is the VM's attached identity, never user input.
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        token = json.load(response)["access_token"]
    request = urllib.request.Request(
        f"https://secretmanager.googleapis.com/v1/{resource}:access",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        payload = json.load(response)["payload"]["data"]
    return environment_values(json.loads(base64.b64decode(payload, validate=True)))


def prepare_runtime(values: dict[str, str], directory: Path) -> None:
    required = {"PYRACAST_SECRET_KEY", "PGUSER", "PGPASSWORD", "RABBITMQ_DEFAULT_PASS"}
    if any(not values.get(key) for key in required):
        raise ValueError("Runtime secret is missing required application, database or broker keys.")
    broker_password = values["RABBITMQ_DEFAULT_PASS"]
    application = {
        key: value for key, value in values.items() if key.startswith(("PYRACAST_", "PG", "EPIAS_"))
    }
    application["PYRACAST_RABBITMQ_URL"] = (
        f"amqp://pyracast:{quote(broker_password, safe='')}@rabbitmq:5672/%2F"
    )
    write_environment(directory / "runtime.env", application)
    write_environment(
        directory / "broker.env",
        {"RABBITMQ_DEFAULT_USER": "pyracast", "RABBITMQ_DEFAULT_PASS": broker_password},
    )


def prepare_database(payload: object, directory: Path) -> None:
    values = environment_values(payload)
    if set(values) != {"POSTGRES_PASSWORD"} or not values["POSTGRES_PASSWORD"]:
        raise ValueError("Database secret must contain only POSTGRES_PASSWORD.")
    write_environment(directory / "database.env", values)


def prepare_migration(payload: object, directory: Path) -> None:
    values = environment_values(payload)
    if set(values) != {"PGUSER", "PGPASSWORD"} or not all(values.values()):
        raise ValueError("Migration secret must contain only PGUSER and PGPASSWORD.")
    write_environment(directory / "migration.env", values)


def staged_directory(script: Path) -> Path:
    return SECRETS / script.resolve().parents[1].name


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("host_config", type=Path)
    parser.add_argument("--migration", type=Path)
    arguments = parser.parse_args(argv)
    directory = staged_directory(Path(__file__))
    if arguments.migration is not None:
        prepare_migration(json.loads(arguments.migration.read_text()), directory)
    else:
        config: dict[str, Any] = json.loads(arguments.host_config.read_text())
        prepare_runtime(access_secret(config["runtime_secret_version"]), directory)
        prepare_database(access_secret(config["database_secret_version"]), directory)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Secret preparation failed; check configuration, IAM and secret format."
        ) from None
