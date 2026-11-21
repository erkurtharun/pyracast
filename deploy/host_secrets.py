import argparse
import getpass
import json
import os
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import host_contract
from deploy.runtime_secrets import database_files, environment_values, migration_files

DEFAULT_DIRECTORY = Path(__file__).resolve().parents[1] / "secrets"
RUNTIME, MIGRATION, DATABASE = "runtime.json", "migration.json", "database.json"
FILES = (RUNTIME, MIGRATION, DATABASE)
MANAGED = frozenset(
    {
        "PYRACAST_SECRET_KEY",
        "PYRACAST_SECRET_KEY_FALLBACKS",
        "PGUSER",
        "PGPASSWORD",
        "RABBITMQ_DEFAULT_PASS",
    }
)
PAIRS = (
    ("PYRACAST_GOOGLE_CLIENT_ID", "PYRACAST_GOOGLE_CLIENT_SECRET"),
    ("PYRACAST_MICROSOFT_CLIENT_ID", "PYRACAST_MICROSOFT_CLIENT_SECRET"),
)
UNROTATED = {
    "broker-password": "RABBITMQ_DEFAULT_PASS",
    "database-password": "POSTGRES_PASSWORD",
}


class RefusedError(Exception):
    pass


def write_new(path: Path, values: dict[str, str]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(values, stream, indent=2)


def replace(path: Path, values: dict[str, str]) -> None:
    staged = path.with_name(f".{path.name}.next")
    staged.unlink(missing_ok=True)
    try:
        write_new(staged, values)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def read(path: Path) -> dict[str, object]:
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        values = None
    if not isinstance(values, dict):
        raise RefusedError(f"{path.name} is missing or not a JSON object.")
    return values


def environment(directory: Path, name: str) -> dict[str, str]:
    try:
        return environment_values(read(directory / name))
    except ValueError as error:
        raise RefusedError(f"{name}: {error}") from None


def mail_configuration() -> dict[str, str]:
    backend = input("Mail backend [gmail/smtp] (gmail): ").strip() or "gmail"
    if backend not in {"gmail", "smtp"}:
        raise RefusedError("Choose gmail or smtp.")
    values = {
        "PYRACAST_EMAIL_BACKEND": backend,
        "PYRACAST_FROM_EMAIL": input("Sender mailbox (info@pyracast.app): ").strip()
        or "info@pyracast.app",
    }
    if backend == "smtp":
        relay = input("SMTP host: ").strip()
        if not relay:
            raise RefusedError("Enter the SMTP host.")
        values |= {
            "PYRACAST_EMAIL_HOST": relay,
            "PYRACAST_EMAIL_PORT": "587",
            "PYRACAST_EMAIL_TLS": "1",
            "PYRACAST_EMAIL_SSL": "0",
            "PYRACAST_EMAIL_USER": input("SMTP username: ").strip(),
            "PYRACAST_EMAIL_PASSWORD": getpass.getpass("SMTP password: "),
        }
    return values


def create(directory: Path) -> None:
    paths = [directory / name for name in FILES]
    if any(path.exists() for path in paths):
        raise RefusedError("Secret files already exist. Preserve the existing encryption key.")
    runtime = {
        "PYRACAST_SECRET_KEY": secrets.token_urlsafe(64),
        "RABBITMQ_DEFAULT_PASS": secrets.token_urlsafe(32),
        "PGUSER": host_contract.RUNTIME_ROLE,
        "PGPASSWORD": secrets.token_urlsafe(32),
        "PYRACAST_WEATHER_ENABLED": "0",
        **mail_configuration(),
    }
    migration = {"PGUSER": host_contract.OWNER_ROLE, "PGPASSWORD": secrets.token_urlsafe(32)}
    database = {"POSTGRES_PASSWORD": secrets.token_urlsafe(32)}
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        for path, values in zip(paths, (runtime, migration, database), strict=True):
            write_new(path, values)
            written.append(path)
    except BaseException:
        for path in written:
            path.unlink()
        raise
    print(f"Created {', '.join(FILES[:2])} and {FILES[2]} in {directory}.")
    if runtime["PYRACAST_EMAIL_BACKEND"] == "gmail":
        print("Authorize gmail.send with deploy/authorize_gmail.py before uploading runtime.json.")


def runtime_problems(payload: dict[str, object]) -> list[str]:
    try:
        values = environment_values(payload)
    except ValueError as error:
        return [f"{RUNTIME}: {error}"]
    problems = [
        f"{RUNTIME}: {key} is missing."
        for key in sorted(host_contract.RUNTIME_REQUIRED)
        if not values.get(key)
    ]
    problems += [
        f"{RUNTIME}: {key} is not a setting the application reads."
        for key in sorted(set(values) - host_contract.RUNTIME_SETTINGS)
    ]
    if values.get("PGUSER", host_contract.RUNTIME_ROLE) != host_contract.RUNTIME_ROLE:
        problems.append(f"{RUNTIME}: PGUSER must be {host_contract.RUNTIME_ROLE}.")
    problems += [
        f"{RUNTIME}: set {first} and {second} together or neither."
        for first, second in PAIRS
        if bool(values.get(first, "").strip()) != bool(values.get(second, "").strip())
    ]
    return problems


def file_problems(name: str, payload: dict[str, object]) -> list[str]:
    try:
        if name == MIGRATION:
            values = migration_files(payload)["migration.env"]
            if values["PGUSER"] != host_contract.OWNER_ROLE:
                return [f"{name}: PGUSER must be {host_contract.OWNER_ROLE}."]
        else:
            database_files(payload)
    except ValueError as error:
        return [f"{name}: {error}"]
    return []


def check(directory: Path) -> list[str]:
    problems: list[str] = []
    found: dict[str, dict[str, object]] = {}
    for name in FILES:
        try:
            found[name] = read(directory / name)
        except RefusedError as refused:
            problems.append(str(refused))
    if RUNTIME in found:
        problems += runtime_problems(found[RUNTIME])
    problems += [
        problem
        for name in (MIGRATION, DATABASE)
        if name in found
        for problem in file_problems(name, found[name])
    ]
    passwords = [found.get(name, {}).get("PGPASSWORD") for name in (RUNTIME, MIGRATION)]
    if passwords[0] and passwords[0] == passwords[1]:
        problems.append(f"{RUNTIME} and {MIGRATION} must not share a database password.")
    return problems


def rotate(directory: Path, target: str) -> None:
    if target in UNROTATED:
        raise RefusedError(
            f"{UNROTATED[target]} only initializes a new data directory; a release does not apply "
            "a new value to the running service. Change it in a planned maintenance instead."
        )
    name = MIGRATION if target == "owner-password" else RUNTIME
    values = environment(directory, name)
    if target == "secret-key":
        if not values.get("PYRACAST_SECRET_KEY", "").strip():
            raise RefusedError(f"{RUNTIME}: PYRACAST_SECRET_KEY is missing.")
        previous = [
            values["PYRACAST_SECRET_KEY"],
            values.get("PYRACAST_SECRET_KEY_FALLBACKS", ""),
        ]
        values["PYRACAST_SECRET_KEY_FALLBACKS"] = " ".join(" ".join(previous).split())
        values["PYRACAST_SECRET_KEY"] = secrets.token_urlsafe(64)
        advice = (
            "The previous key is the first of PYRACAST_SECRET_KEY_FALLBACKS. After the release, "
            "run pyracast reencrypt_secrets --verify; once pyracast check_secrets "
            "--require-primary exits 0, run drop-fallbacks and release that version."
        )
    else:
        values["PGPASSWORD"] = secrets.token_urlsafe(32)
        advice = "The next release sets the role's password from this value."
    replace(directory / name, values)
    print(f"Rotated {target} in {name}. {advice}")


def drop_fallbacks(directory: Path) -> None:
    values = environment(directory, RUNTIME)
    if not values.pop("PYRACAST_SECRET_KEY_FALLBACKS", "").strip():
        raise RefusedError(f"{RUNTIME} has no PYRACAST_SECRET_KEY_FALLBACKS to remove.")
    replace(directory / RUNTIME, values)
    print(
        f"Removed PYRACAST_SECRET_KEY_FALLBACKS from {RUNTIME}. A release refuses, before "
        "maintenance, a key set that cannot read a stored secret."
    )


def store(directory: Path, key: str) -> None:
    if key in MANAGED:
        raise RefusedError(f"{key} is managed by rotate.")
    if key not in host_contract.RUNTIME_SETTINGS:
        raise RefusedError(f"{key} is not a setting the application reads.")
    values = environment(directory, RUNTIME)
    value = getpass.getpass(f"{key} (not shown): ")
    if not value or any(character in value for character in "\r\n\x00"):
        raise RefusedError("Enter a value of one line.")
    replace(directory / RUNTIME, values | {key: value})
    print(f"Stored {key} in {RUNTIME}.")


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create, check and rotate the host secret files; no value is ever printed."
    )
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create", help="write runtime, migration and database secret files")
    commands.add_parser("check", help="check the three files against the host contract")
    rotation = commands.add_parser("rotate", help="replace one generated value")
    rotation.add_argument(
        "target",
        choices=("secret-key", "runtime-password", "owner-password", *UNROTATED),
    )
    commands.add_parser(
        "drop-fallbacks",
        help="finish a secret-key rotation once check_secrets --require-primary exits 0",
    )
    setting = commands.add_parser("set", help="store one runtime setting typed without echo")
    setting.add_argument("key")
    options = parser.parse_args(arguments)
    try:
        if options.command == "create":
            create(options.directory)
        elif options.command == "check":
            problems = check(options.directory)
            for problem in problems:
                print(problem, file=sys.stderr)
            if problems:
                return 1
            print(f"{', '.join(FILES[:2])} and {FILES[2]} are complete.")
        elif options.command == "rotate":
            rotate(options.directory, options.target)
        elif options.command == "drop-fallbacks":
            drop_fallbacks(options.directory)
        else:
            store(options.directory, options.key)
    except RefusedError as refused:
        print(refused, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
