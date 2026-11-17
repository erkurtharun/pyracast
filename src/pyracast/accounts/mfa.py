from allauth.mfa.adapter import DefaultMFAAdapter
from allauth.mfa.models import Authenticator
from cryptography.fernet import InvalidToken
from django.core.exceptions import PermissionDenied
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.authorization import requires_admin_mfa
from pyracast.encryption import secret_cipher

PREFIX = "mfa:v1:"
KEY_CONTEXT = b"pyracast-mfa-credentials-v1"


class MFAAdapter(DefaultMFAAdapter):
    def can_delete_authenticator(self, authenticator: Authenticator) -> bool:
        user = authenticator.user
        if authenticator.type == Authenticator.Type.TOTP and requires_admin_mfa(user):
            return False
        return super().can_delete_authenticator(authenticator)

    @sensitive_variables()
    def encrypt(self, text: str) -> str:
        return PREFIX + secret_cipher(KEY_CONTEXT).encrypt(text.encode()).decode("ascii")

    @sensitive_variables()
    def decrypt(self, encrypted_text: str) -> str:
        if not encrypted_text.startswith(PREFIX):
            raise PermissionDenied("Authenticator could not be verified.")
        try:
            return secret_cipher(KEY_CONTEXT).decrypt(encrypted_text[len(PREFIX) :]).decode()
        except InvalidToken, ValueError:
            raise PermissionDenied("Authenticator could not be verified.") from None
