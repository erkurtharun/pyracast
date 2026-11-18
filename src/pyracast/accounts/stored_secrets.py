from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal

from allauth.mfa.models import Authenticator
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.mfa import decrypt_payload
from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import ProviderAccount
from pyracast.connections.secrets import decrypt_api_key

PROVIDERS = ProviderAccount._meta.db_table
AUTHENTICATORS = Authenticator._meta.db_table
ENCRYPTED_AUTHENTICATORS = (Authenticator.Type.TOTP, Authenticator.Type.RECOVERY_CODES)
type Readability = Literal["primary", "fallback", "unreadable"]


@dataclass(frozen=True)
class StoredSecret:
    table: str
    pk: int
    # None marks a shape that holds no readable ciphertext.
    tokens: tuple[str, ...] | None
    decrypt: Callable[[str, bool], str]

    @property
    def row(self) -> str:
        return f"{self.table} {self.pk}"


def _provider(token: str, primary_only: bool) -> str:
    return decrypt_api_key(token, primary_only=primary_only)


def _authenticator(token: str, primary_only: bool) -> str:
    return decrypt_payload(token, primary_only=primary_only)


def authenticator_tokens(kind: str, data: object) -> tuple[str, ...] | None:
    if not isinstance(data, dict):
        return None
    if kind == Authenticator.Type.TOTP:
        values: object = [data.get("secret")]
    elif "migrated_codes" in data:
        values = data["migrated_codes"]
    else:
        values = [data.get("seed")]
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return None
    return tuple(values)


def stored_secrets() -> Iterator[StoredSecret]:
    providers = ProviderAccount.objects.exclude(encrypted_api_key="").order_by("pk")
    for pk, token in providers.values_list("pk", "encrypted_api_key"):
        yield StoredSecret(PROVIDERS, pk, (token,), _provider)
    authenticators = Authenticator.objects.filter(type__in=ENCRYPTED_AUTHENTICATORS).order_by("pk")
    for pk, kind, data in authenticators.values_list("pk", "type", "data"):
        yield StoredSecret(AUTHENTICATORS, pk, authenticator_tokens(kind, data), _authenticator)


@sensitive_variables()
def decrypts(secret: StoredSecret, *, primary_only: bool) -> bool:
    try:
        for token in secret.tokens or ():
            secret.decrypt(token, primary_only)
    except ProviderError, PermissionDenied:
        return False
    return True


def readability(secret: StoredSecret) -> Readability:
    if secret.tokens is None or not decrypts(secret, primary_only=False):
        return "unreadable"
    return "primary" if decrypts(secret, primary_only=True) else "fallback"


def tables_exist() -> bool:
    return {PROVIDERS, AUTHENTICATORS} <= set(connection.introspection.table_names())
