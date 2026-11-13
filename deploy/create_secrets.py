import getpass
import json
import os
import secrets
from pathlib import Path


def mail_configuration() -> dict[str, str]:
    backend = input("Mail backend [gmail/smtp] (gmail): ").strip() or "gmail"
    if backend not in {"gmail", "smtp"}:
        raise SystemExit("Choose gmail or smtp.")
    values = {
        "PYRACAST_EMAIL_BACKEND": backend,
        "PYRACAST_FROM_EMAIL": input("Sender mailbox (info@pyracast.app): ").strip()
        or "info@pyracast.app",
    }
    if backend == "smtp":
        values.update(
            {
                "PYRACAST_EMAIL_HOST": input("SMTP host: ").strip(),
                "PYRACAST_EMAIL_PORT": "587",
                "PYRACAST_EMAIL_TLS": "1",
                "PYRACAST_EMAIL_SSL": "0",
                "PYRACAST_EMAIL_USER": input("SMTP username: ").strip(),
                "PYRACAST_EMAIL_PASSWORD": getpass.getpass("SMTP password: "),
            }
        )
    return values


def main() -> None:
    directory = Path("secrets")
    directory.mkdir(mode=0o700, exist_ok=True)
    paths = [directory / name for name in ("runtime.json", "migration.json", "database.json")]
    if any(path.exists() for path in paths):
        raise SystemExit("Secret files already exist. Preserve the existing encryption key.")
    runtime = {
        "PYRACAST_SECRET_KEY": secrets.token_urlsafe(64),
        "RABBITMQ_DEFAULT_PASS": secrets.token_urlsafe(32),
        "PGUSER": "pyracast_app",
        "PGPASSWORD": secrets.token_urlsafe(32),
        "PYRACAST_WEATHER_ENABLED": "0",
        **mail_configuration(),
    }
    migration = {
        "PGUSER": "pyracast_owner",
        "PGPASSWORD": secrets.token_urlsafe(32),
    }
    if any(not value for value in [runtime["PGPASSWORD"], migration["PGPASSWORD"]]):
        raise SystemExit("Database passwords cannot be empty.")
    database = {"POSTGRES_PASSWORD": secrets.token_urlsafe(32)}
    for path, values in zip(paths, [runtime, migration, database], strict=True):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(values, stream, indent=2)
    print("Created runtime.json, migration.json and database.json in secrets/.")
    if runtime["PYRACAST_EMAIL_BACKEND"] == "gmail":
        print("Authorize gmail.send with deploy/authorize_gmail.py before uploading runtime.json.")


if __name__ == "__main__":
    main()
