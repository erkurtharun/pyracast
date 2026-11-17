from allauth.account.auth_backends import AuthenticationBackend as AllauthAuthenticationBackend
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import User


def email_identity(email: str, *, for_recovery: bool = False) -> User | None:
    matches = (
        EmailAddress.objects.filter(email__iexact=email).select_related("user").order_by("user_id")
    )
    verified = list(matches.filter(verified=True))
    if verified:
        return verified[0].user if len(verified) == 1 else None
    initial = [
        address
        for address in matches.filter(primary=True)
        if not EmailAddress.objects.filter(user_id=address.user_id).exclude(pk=address.pk).exists()
    ]
    if for_recovery:
        return next((address.user for address in initial if address.user.is_active), None)
    return initial[0].user if len(initial) == 1 else None


def may_sign_in_with_password(user: User) -> bool:
    if not user.is_active:
        return False
    return (
        not (user.is_staff or user.is_superuser)
        or Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).exists()
    )


class AuthenticationBackend(AllauthAuthenticationBackend):
    # A refused account must look like a wrong password to allauth: returning None keeps the
    # login_failed hit, while the base class would stash the user and answer "inactive".
    def _authenticate_by_email(self, email: str, password: str) -> User | None:
        user = email_identity(email)
        if user is None:
            return None
        self._did_check_password = True
        if not user.check_password(password) or not may_sign_in_with_password(user):
            return None
        return user
