from typing import Any

from allauth.mfa.models import Authenticator
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.mfa import MFAAdapter
from pyracast.accounts.stored_secrets import (
    ENCRYPTED_AUTHENTICATORS,
    PROVIDERS,
    readability,
    stored_secrets,
)
from pyracast.connections.models import ProviderAccount
from pyracast.connections.secrets import decrypt_api_key, encrypt_secret


class SecretRotationError(Exception):
    def __init__(self, reason: str, rows: list[str], returncode: int) -> None:
        super().__init__(f"{reason}: {', '.join(rows)}.")
        self.returncode = returncode


class Command(BaseCommand):
    help = (
        "Re-encrypt provider and MFA secrets with the current primary application key in one "
        "transaction. Exit 1: a secret is unreadable or malformed; exit 2: --verify found a "
        "secret the primary key alone cannot read. Either way nothing is committed."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Before committing, prove that every secret decrypts with the primary key alone.",
        )

    @sensitive_variables()
    def handle(self, *args: Any, **options: Any) -> None:
        try:
            providers, authenticators = reencrypt_secrets(verify=options["verify"])
        except SecretRotationError as error:
            raise CommandError(
                f"{error} No changes were committed; retain the fallback keys.",
                returncode=error.returncode,
            ) from None
        self.stdout.write(
            f"Re-encrypted {providers} provider credentials "
            f"and {authenticators} MFA authenticators."
        )
        if options["verify"]:
            self.stdout.write("Verified: every stored secret decrypts with the primary key alone.")


@sensitive_variables()
@transaction.atomic
def reencrypt_secrets(*, verify: bool = False) -> tuple[int, int]:
    accounts = list(ProviderAccount.objects.select_for_update().exclude(encrypted_api_key=""))
    authenticators = list(
        Authenticator.objects.select_for_update().filter(type__in=ENCRYPTED_AUTHENTICATORS)
    )
    unreadable = [secret.row for secret in stored_secrets() if readability(secret) == "unreadable"]
    if unreadable:
        raise SecretRotationError("Unreadable or malformed secrets", unreadable, 1)
    for account in accounts:
        try:
            account.encrypted_api_key = encrypt_secret(decrypt_api_key(account.encrypted_api_key))
        except ValidationError:
            raise SecretRotationError(
                "Unreadable or malformed secrets", [f"{PROVIDERS} {account.pk}"], 1
            ) from None
        account.save(update_fields=["encrypted_api_key"])
    adapter = MFAAdapter()
    for authenticator in authenticators:
        data = authenticator.data.copy()
        if authenticator.type == Authenticator.Type.TOTP:
            data["secret"] = adapter.encrypt(adapter.decrypt(data["secret"]))
        elif "migrated_codes" in data:
            data["migrated_codes"] = [
                adapter.encrypt(adapter.decrypt(code)) for code in data["migrated_codes"]
            ]
        else:
            data["seed"] = adapter.encrypt(adapter.decrypt(data["seed"]))
        authenticator.data = data
        authenticator.save(update_fields=["data"])
    if verify:
        failed = [secret.row for secret in stored_secrets() if readability(secret) != "primary"]
        if failed:
            raise SecretRotationError("Not readable with the primary key alone", failed, 2)
    return len(accounts), len(authenticators)
