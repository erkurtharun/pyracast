import argparse
import json
import os
import re
import ssl
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

# Compose reads release.env literally; a value outside this set could break or extend it.
VALUE = re.compile(r"[a-zA-Z0-9@.:/_+%-]+")
CERTIFICATE = r"-----BEGIN CERTIFICATE-----\n[A-Za-z0-9+/=\n]+-----END CERTIFICATE-----\n"
CERTIFICATES = re.compile(rf"(?:{CERTIFICATE}\n?)+")
ORIGIN_PULL_CA = "origin-pull-ca.pem"


def _loads_as_x509(certificate: str) -> bool:
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=certificate)
    except ssl.SSLError:
        return False
    return True


def origin_pull_authority(config: Mapping[str, Any]) -> str | None:
    value = config.get("origin_pull_ca", "")
    if value == "":
        return None
    authority = value.strip() + "\n" if isinstance(value, str) else None
    if (
        authority is None
        or CERTIFICATES.fullmatch(authority) is None
        or not all(map(_loads_as_x509, re.findall(CERTIFICATE, authority)))
    ):
        raise ValueError(
            "origin_pull_ca must hold only PEM X.509 certificates of the edge's authority."
        )
    return authority


def render(
    config: Mapping[str, Any], secrets: str, images: Sequence[str], deploy: Path
) -> dict[str, str]:
    backend, web, gateway, postgres, rabbitmq = images
    values = {
        "BACKEND_IMAGE": backend,
        "WEB_IMAGE": web,
        "GATEWAY_IMAGE": gateway,
        "POSTGRES_IMAGE": postgres,
        "RABBITMQ_IMAGE": rabbitmq,
        "SITE_ADDRESS": config["domain"],
        "ACME_EMAIL": config["acme_email"],
        "RUNTIME_ENV_FILE": f"{secrets}/runtime.env",
        "BROKER_ENV_FILE": f"{secrets}/broker.env",
        "DATABASE_ENV_FILE": f"{secrets}/database.env",
        "MIGRATION_ENV_FILE": f"{secrets}/migration.env",
        "ORIGIN_PULL": "off",
    }
    if any(not isinstance(value, str) or not VALUE.fullmatch(value) for value in values.values()):
        raise ValueError("Invalid nonsecret deployment configuration.")
    authority = origin_pull_authority(config)
    if authority is not None:
        stored = deploy / ORIGIN_PULL_CA
        stored.write_text(authority, encoding="utf-8")
        # The gateway reads the bind-mounted file as its own unprivileged user.
        stored.chmod(0o644)
        values |= {"ORIGIN_PULL": "required", "ORIGIN_PULL_CA": stored.as_posix()}
    return values


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("host_config", type=Path)
    parser.add_argument("release_env", type=Path)
    parser.add_argument("secrets")
    parser.add_argument("images", nargs=5)
    options = parser.parse_args(arguments)
    config = json.loads(options.host_config.read_text(encoding="utf-8"))
    values = render(config, options.secrets, options.images, options.release_env.parent)
    with open(options.release_env, "w", encoding="utf-8", newline="\n") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == "__main__":
    main()
