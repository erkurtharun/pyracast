# ruff: noqa: S603 -- Fixed Docker executables with argument lists; no shell interpolation.
import argparse
import base64
import hashlib
import hmac
import os
import re
import stringprep
import subprocess
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path

RUNTIME_ROLE = "pyracast_app"
ROLES = {"runtime.env": RUNTIME_ROLE, "migration.env": "pyracast_owner"}
# PostgreSQL's default. The client computes the verifier the server would, so the plaintext never
# reaches the server or its logs.
ITERATIONS = 4096
PROHIBITED = (
    stringprep.in_table_a1,
    stringprep.in_table_c12,
    stringprep.in_table_c21_c22,
    stringprep.in_table_c3,
    stringprep.in_table_c4,
    stringprep.in_table_c5,
    stringprep.in_table_c6,
    stringprep.in_table_c7,
    stringprep.in_table_c8,
    stringprep.in_table_c9,
)
VERIFIER = re.compile(
    r"SCRAM-SHA-256\$(\d+):([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+):([A-Za-z0-9+/=]+)"
)
MD5_VERIFIER = re.compile(r"md5[0-9a-f]{32}")
STORED_VERIFIERS = (
    "SELECT rolname || ' ' || coalesce(rolpassword, '') FROM pg_authid "
    "WHERE rolname IN ('pyracast_owner', 'pyracast_app')"
)


def read_environment(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line)


def saslprep(password: str) -> str:
    # PostgreSQL's pg_saslprep: ASCII passes unchanged, and a password SASLprep (RFC 4013) refuses
    # is hashed as given.
    if password.isascii():
        return password
    mapped = "".join(
        " " if stringprep.in_table_c12(character) else character
        for character in password
        if stringprep.in_table_c12(character) or not stringprep.in_table_b1(character)
    )
    prepared = unicodedata.ucd_3_2_0.normalize("NFKC", mapped)
    if not prepared or any(table(character) for character in prepared for table in PROHIBITED):
        return password
    if any(stringprep.in_table_d1(character) for character in prepared) and (
        any(stringprep.in_table_d2(character) for character in prepared)
        or not stringprep.in_table_d1(prepared[0])
        or not stringprep.in_table_d1(prepared[-1])
    ):
        return password
    return prepared


def scram_keys(password: str, salt: bytes, iterations: int) -> tuple[bytes, bytes]:
    salted = hashlib.pbkdf2_hmac("sha256", saslprep(password).encode(), salt, iterations)
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
    return [
        f"ALTER ROLE {role} PASSWORD '{scram_verifier(password, salt())}';"
        for role, password in sorted(passwords.items())
        if not scram_matches(stored.get(role), password)
    ]


def role_passwords(directory: Path, names: Mapping[str, str]) -> dict[str, str]:
    passwords = {}
    for name, role in names.items():
        values = read_environment(directory / name)
        if values.get("PGUSER") != role:
            raise ValueError("Database role names must match the provisioning definition.")
        passwords[role] = values["PGPASSWORD"]
    return passwords


def psql_command(release: Path) -> list[str]:
    return [
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


def stored_verifiers(psql: list[str]) -> dict[str, str | None]:
    listed = subprocess.run(
        [*psql, "-Atc", STORED_VERIFIERS], check=True, capture_output=True, text=True
    )
    return {
        role: verifier or None
        for role, _, verifier in (line.partition(" ") for line in listed.stdout.splitlines())
    }


def apply(psql: list[str], statements: str) -> None:
    subprocess.run(
        [*psql, "--single-transaction"],
        input=statements,
        text=True,
        check=True,
        capture_output=True,
    )


def converge_database(release: Path, directory: Path) -> list[str]:
    # The runtime role's password changes in switch_runtime, once the serving release stopped.
    passwords = role_passwords(directory, ROLES)
    del passwords[RUNTIME_ROLE]
    psql = psql_command(release)
    changes = password_statements(stored_verifiers(psql), passwords)
    statements = (release / "deploy/bootstrap.sql").read_text()
    apply(psql, statements + "".join(f"{line}\n" for line in changes))
    return [change.split()[2] for change in changes]


def switch_runtime(release: Path, directory: Path, saved: Path) -> bool:
    passwords = role_passwords(directory, {"runtime.env": RUNTIME_ROLE})
    psql = psql_command(release)
    previous = stored_verifiers(psql).get(RUNTIME_ROLE)
    changes = password_statements({RUNTIME_ROLE: previous}, passwords)
    if not changes:
        return False
    if not saved.exists():
        saved.write_text(previous or "")
    apply(psql, "".join(f"{line}\n" for line in changes))
    return True


def revert_runtime(release: Path, saved: Path) -> bool:
    if not saved.exists():
        return False
    verifier = saved.read_text()
    if verifier and not (VERIFIER.fullmatch(verifier) or MD5_VERIFIER.fullmatch(verifier)):
        raise ValueError("The saved runtime password is not a PostgreSQL verifier.")
    password = f"'{verifier}'" if verifier else "NULL"
    apply(psql_command(release), f"ALTER ROLE {RUNTIME_ROLE} PASSWORD {password};\n")
    saved.unlink()
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    steps = parser.add_subparsers(dest="step", required=True)
    converge = steps.add_parser("converge")
    converge.add_argument("release", type=Path)
    converge.add_argument("environment_directory", type=Path)
    switch = steps.add_parser("switch-runtime")
    switch.add_argument("release", type=Path)
    switch.add_argument("environment_directory", type=Path)
    switch.add_argument("saved", type=Path)
    revert = steps.add_parser("revert-runtime")
    revert.add_argument("release", type=Path)
    revert.add_argument("saved", type=Path)
    arguments = parser.parse_args()
    if arguments.step == "converge":
        for role in converge_database(arguments.release, arguments.environment_directory):
            print(role)
    elif arguments.step == "switch-runtime":
        switch_runtime(arguments.release, arguments.environment_directory, arguments.saved)
    else:
        revert_runtime(arguments.release, arguments.saved)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Database provisioning failed; inspect database state and secret configuration."
        ) from None
