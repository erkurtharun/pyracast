import re

from cryptography.fernet import InvalidToken
from django.core.exceptions import ValidationError
from django.views.decorators.debug import sensitive_variables

from pyracast.connections.contracts import ProviderError
from pyracast.encryption import secret_cipher

PREFIX = "encrypted:v1:"
KEY_CONTEXT = b"pyracast-provider-credentials-v1"


@sensitive_variables()
def encrypt_secret(raw: str) -> str:
    if not re.fullmatch(r"[!-~]{1,1024}", raw):
        raise ValidationError("Enter an API key without spaces, up to 1024 characters.")
    return PREFIX + secret_cipher(KEY_CONTEXT).encrypt(raw.encode()).decode("ascii")


@sensitive_variables()
def decrypt_api_key(encrypted: str) -> str:
    if not encrypted.startswith(PREFIX) or len(encrypted) > 2048:
        raise ProviderError("invalid_provider_secret")
    try:
        return secret_cipher(KEY_CONTEXT).decrypt(encrypted[len(PREFIX) :]).decode("utf-8")
    except InvalidToken, ValueError:
        raise ProviderError("invalid_provider_secret") from None
