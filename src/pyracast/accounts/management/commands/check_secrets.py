from collections import Counter
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from pyracast.accounts.stored_secrets import (
    AUTHENTICATORS,
    PROVIDERS,
    readability,
    stored_secrets,
    tables_exist,
)


class Command(BaseCommand):
    help = (
        "Prove that every stored provider and MFA secret decrypts with the configured keys. "
        "Exit 1: a secret is unreadable or malformed; exit 2: --require-primary and a secret "
        "still needs a fallback key."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--require-primary",
            action="store_true",
            help="Also require every secret to decrypt with the primary key alone.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not tables_exist():
            self.stdout.write("No stored secrets: the tables do not exist yet.")
            return
        counts = {PROVIDERS: Counter[str](), AUTHENTICATORS: Counter[str]()}
        unreadable = []
        for secret in stored_secrets():
            state = readability(secret)
            counts[secret.table][state] += 1
            if state == "unreadable":
                unreadable.append(f"Unreadable: {secret.row}")
        for table, counted in counts.items():
            self.stdout.write(
                f"{table}: {counted['primary']} primary, {counted['fallback']} fallback, "
                f"{counted['unreadable']} unreadable"
            )
        if unreadable:
            raise CommandError(
                "\n".join([*unreadable, "These secrets do not decrypt with the configured keys."]),
                returncode=1,
            )
        if options["require_primary"] and any(counted["fallback"] for counted in counts.values()):
            raise CommandError(
                "Some secrets decrypt only with a fallback key; re-encrypt them with "
                "reencrypt_secrets --verify before removing that key.",
                returncode=2,
            )
