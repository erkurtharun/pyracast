import base64

from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.utils.encoding import force_bytes
from django.views.decorators.debug import sensitive_variables


@sensitive_variables()
def derived_keys(context: bytes, *, primary_only: bool = False) -> list[bytes]:
    """One 32-byte key per configured application key, primary first."""
    keys = (
        (settings.SECRET_KEY,)
        if primary_only
        else (settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS)
    )
    return [
        HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=context).derive(force_bytes(key))
        for key in keys
    ]


@sensitive_variables()
def secret_cipher(context: bytes, *, primary_only: bool = False) -> MultiFernet:
    return MultiFernet(
        [
            Fernet(base64.urlsafe_b64encode(key))
            for key in derived_keys(context, primary_only=primary_only)
        ]
    )
