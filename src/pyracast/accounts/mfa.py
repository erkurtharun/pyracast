from allauth.account.internal.stagekit import unstash_login
from allauth.core import context
from allauth.mfa.adapter import DefaultMFAAdapter
from allauth.mfa.models import Authenticator
from cryptography.fernet import InvalidToken
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.accounts.authorization import requires_admin_mfa
from pyracast.encryption import secret_cipher

PREFIX = "mfa:v1:"
KEY_CONTEXT = b"pyracast-mfa-credentials-v1"


def _factor_owner_id(request: HttpRequest | None) -> int | None:
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return int(user.pk)
    if request is None or not hasattr(request, "session"):
        return None
    # During sign-in the factor belongs to the user of the pending login.
    login = unstash_login(request, peek=True)
    return None if login is None or login.user is None else int(login.user.pk)


def _unreadable() -> PermissionDenied:
    request = context.request
    record_security_event(
        SecurityEvent.AUTHENTICATOR_UNREADABLE,
        request=request,
        user_id=_factor_owner_id(request),
    )
    return PermissionDenied("Authenticator could not be verified.")


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
            raise _unreadable()
        try:
            return secret_cipher(KEY_CONTEXT).decrypt(encrypted_text[len(PREFIX) :]).decode()
        except InvalidToken, ValueError:
            raise _unreadable() from None
