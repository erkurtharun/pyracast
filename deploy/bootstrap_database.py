# ruff: noqa: S603 -- Fixed Docker executables with argument lists; no shell interpolation.
import argparse
import base64
import hashlib
import hmac
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

ROLES = {"runtime.env": "pyracast_app", "migration.env": "pyracast_owner"}
# PostgreSQL's default; SASLprep leaves printable ASCII unchanged, so the client computes the same
# verifier the server would and the plaintext never reaches the server or its logs.
ITERATIONS = 4096
PRINTABLE_ASCII = re.compile(r"[!-~]+")
VERIFIER = re.compile(
    r"SCRAM-SHA-256\$(\d+):([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+):([A-Za-z0-9+/=]+)"
)
STORED_VERIFIERS = (
    "SELECT rolname || ' ' || coalesce(rolpassword, '') FROM pg_authid "
    "WHERE rolname IN ('pyracast_owner', 'pyracast_app')"
)


def read_environment(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line)


def scram_keys(password: str, salt: bytes, iterations: int) -> tuple[bytes, bytes]:
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client = hmac.digest(salted, b"Client Key", "sha256")
    return hashlib.sha256(client).digest(), hmac.digest(salted, b"Server Key", "sha256")


def scram_verifier(password: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    stored, server = scram_keys(password, salt, iterations)
    encoded = (base64.b64encode(value).decode() for value in (salt, stored, server))
    return "SCRAM-SHA-256${}:{}${}:{}".format(iterations, *encoded)


def scram_matches(verifier: str | None, password: str) -> bool:
    match = VERIFIER.fullmatch(verifier or "")
    if match is None:
        return False
    try:
        salt, stored, server = (
            base64.b64decode(value, validate=True) for value in match.groups()[1:]
        )
    except ValueError:
        return False
    expected = scram_keys(password, salt, int(match[1]))
    return hmac.compare_digest(expected[0], stored) and hmac.compare_digest(expected[1], server)


def password_statements(
    stored: Mapping[str, str | None],
    passwords: dict[str, str],
    salt: Callable[[], bytes] = lambda: os.urandom(16),
) -> list[str]:
    if not all(PRINTABLE_ASCII.fullmatch(password) for password in passwords.values()):
        raise ValueError("Database passwords must be printable ASCII without spaces.")
    return [
        f"ALTER ROLE {role} PASSWORD '{scram_verifier(password, salt())}';"
        for role, password in sorted(passwords.items())
        if not scram_matches(stored.get(role), password)
    ]


def converge_database(release: Path, directory: Path) -> None:
    passwords = {}
    for name, role in ROLES.items():
        values = read_environment(directory / name)
        if values.get("PGUSER") != role:
            raise ValueError("Database role names must match the provisioning definition.")
        passwords[role] = values["PGPASSWORD"]
    psql = [
        "docker",
        "compose",
        "--env-file",
        str(release / "deploy/release.env"),
        "-f",
        str(release / "deploy/compose.yaml"),
        "exec",
        "-T",
        "postgres",
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        "-d",
        "pyracast",
    ]
    listed = subprocess.run(
        [*psql, "-Atc", STORED_VERIFIERS], check=True, capture_output=True, text=True
    )
    stored = {
        role: verifier or None
        for role, _, verifier in (line.partition(" ") for line in listed.stdout.splitlines())
    }
    statements = (release / "deploy/bootstrap.sql").read_text()
    statements += "".join(f"{line}\n" for line in password_statements(stored, passwords))
    subprocess.run(
        [*psql, "--single-transaction"],
        input=statements,
        text=True,
        check=True,
        capture_output=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("release", type=Path)
    parser.add_argument("environment_directory", type=Path)
    arguments = parser.parse_args()
    converge_database(arguments.release, arguments.environment_directory)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Database provisioning failed; inspect database state and secret configuration."
        ) from None
