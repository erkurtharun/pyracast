import getpass
import json
import os
import secrets
from pathlib import Path


def main() -> None:
    directory = Path("secrets")
    directory.mkdir(mode=0o700, exist_ok=True)
    paths = [directory / "runtime.json", directory / "migration.json"]
    if any(path.exists() for path in paths):
        raise SystemExit("Secret files already exist. Preserve the existing encryption key.")
    runtime = {
        "PYRACAST_SECRET_KEY": secrets.token_urlsafe(64),
        "RABBITMQ_DEFAULT_PASS": secrets.token_urlsafe(32),
        "PGUSER": "pyracast_app",
        "PGPASSWORD": getpass.getpass("Existing pyracast_app database password: "),
        "PYRACAST_EMAIL_HOST": input("SMTP host: ").strip(),
        "PYRACAST_EMAIL_PORT": "587",
        "PYRACAST_EMAIL_TLS": "1",
        "PYRACAST_EMAIL_SSL": "0",
        "PYRACAST_EMAIL_USER": input("SMTP username: ").strip(),
        "PYRACAST_EMAIL_PASSWORD": getpass.getpass("SMTP password: "),
        "PYRACAST_FROM_EMAIL": input("Verified sender email: ").strip(),
        "PYRACAST_WEATHER_ENABLED": "0",
    }
    migration = {
        "PGUSER": "pyracast_owner",
        "PGPASSWORD": getpass.getpass("Existing pyracast_owner database password: "),
    }
    if any(not value for value in [runtime["PGPASSWORD"], migration["PGPASSWORD"]]):
        raise SystemExit("Database passwords cannot be empty.")
    for path, values in zip(paths, [runtime, migration], strict=True):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(values, stream, indent=2)
    print("Created secrets/runtime.json and secrets/migration.json. Upload and store securely.")


if __name__ == "__main__":
    main()
