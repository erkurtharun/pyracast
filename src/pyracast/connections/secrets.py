import hmac
import re
from hashlib import sha256

from cryptography.fernet import InvalidToken
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_variables

from pyracast.connections.contracts import ProviderError
from pyracast.encryption import derived_keys, secret_cipher

PREFIX = "encrypted:v1:"
KEY_CONTEXT = b"pyracast-provider-credentials-v1"
FINGERPRINT_CONTEXT = b"pyracast-provider-key-fingerprint-v1"


@sensitive_variables()
def encrypt_secret(raw: str) -> str:
    if not re.fullmatch(r"[!-~]{1,1024}", raw):
        raise ValidationError(
            _("Enter an API key without spaces, up to 1024 characters."), code="invalid_api_key"
        )
    return PREFIX + secret_cipher(KEY_CONTEXT).encrypt(raw.encode()).decode("ascii")


@sensitive_variables()
def decrypt_api_key(encrypted: str, *, primary_only: bool = False) -> str:
    if not encrypted.startswith(PREFIX) or len(encrypted) > 2048:
        raise ProviderError("invalid_provider_secret")
    cipher = secret_cipher(KEY_CONTEXT, primary_only=primary_only)
    try:
        return cipher.decrypt(encrypted[len(PREFIX) :]).decode("utf-8")
    except InvalidToken, ValueError:
        raise ProviderError("invalid_provider_secret") from None


@sensitive_variables()
def api_key_fingerprints(raw: str) -> tuple[str, ...]:
    """Keyed with the application key, primary first: a stored fingerprint cannot be tested
    against candidate API keys without that key."""
    return tuple(
        hmac.new(key, raw.encode(), sha256).hexdigest() for key in derived_keys(FINGERPRINT_CONTEXT)
    )
