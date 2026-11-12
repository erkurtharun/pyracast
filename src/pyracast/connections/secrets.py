import base64
import os
import re

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.encoding import force_bytes
from django.views.decorators.debug import sensitive_variables

from pyracast.connections.contracts import ProviderError

PREFIX = "encrypted:v1:"


def _cipher() -> MultiFernet:
    keys = [settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS]
    return MultiFernet(
        [
            Fernet(
                base64.urlsafe_b64encode(
                    HKDF(
                        algorithm=hashes.SHA256(),
                        length=32,
                        salt=None,
                        info=b"pyracast-provider-credentials-v1",
                    ).derive(force_bytes(key))
                )
            )
            for key in keys
        ]
    )


@sensitive_variables()
def encrypt_secret(raw: str) -> str:
    if not re.fullmatch(r"[!-~]{1,1024}", raw):
        raise ValidationError("Enter an API key without spaces, up to 1024 characters.")
    return PREFIX + _cipher().encrypt(raw.encode()).decode("ascii")


def validate_secret_reference(reference: str) -> None:
    if reference.startswith("env:") and reference[4:].isidentifier():
        return
    if reference.startswith(PREFIX) and len(reference) <= 2048:
        try:
            resolve_solcast_secret(reference)
            return
        except ProviderError:
            pass
    raise ValidationError("A valid encrypted key or environment secret reference is required.")


@sensitive_variables()
def resolve_solcast_secret(reference: str) -> str:
    if reference.startswith(PREFIX):
        try:
            return _cipher().decrypt(reference[len(PREFIX) :]).decode("utf-8")
        except InvalidToken, UnicodeDecodeError:
            raise ProviderError("invalid_provider_secret") from None
    if not re.fullmatch(r"env:(?:SOLCAST_API_KEY|PYRACAST_SOLCAST_[A-Z0-9_]+)", reference):
        raise ProviderError("invalid_secret_reference")
    secret = os.environ.get(reference[4:])
    if not secret:
        raise ProviderError("missing_provider_secret")
    return secret
