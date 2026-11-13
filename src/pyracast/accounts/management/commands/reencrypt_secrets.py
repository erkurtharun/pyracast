from typing import Any

from allauth.mfa.models import Authenticator
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.mfa import MFAAdapter
from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import ProviderAccount
from pyracast.connections.secrets import decrypt_api_key, encrypt_secret


class Command(BaseCommand):
    help = "Re-encrypt provider and MFA secrets with the current primary application key."

    @sensitive_variables()
    def handle(self, *args: Any, **options: Any) -> None:
        try:
            providers, authenticators = reencrypt_secrets()
        except PermissionDenied, ValidationError, ProviderError, KeyError, TypeError:
            raise CommandError(
                "Secret re-encryption failed. No changes were committed; retain the fallback keys."
            ) from None
        self.stdout.write(
            f"Re-encrypted {providers} provider credentials "
            f"and {authenticators} MFA authenticators."
        )


@sensitive_variables()
@transaction.atomic
def reencrypt_secrets() -> tuple[int, int]:
    providers = 0
    for account in ProviderAccount.objects.select_for_update().exclude(encrypted_api_key=""):
        account.encrypted_api_key = encrypt_secret(decrypt_api_key(account.encrypted_api_key))
        account.save(update_fields=["encrypted_api_key"])
        providers += 1
    adapter = MFAAdapter()
    authenticators = 0
    for authenticator in Authenticator.objects.select_for_update().filter(
        type__in=[Authenticator.Type.TOTP, Authenticator.Type.RECOVERY_CODES]
    ):
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
        authenticators += 1
    return providers, authenticators
