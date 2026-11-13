from allauth.account.auth_backends import AuthenticationBackend as AllauthAuthenticationBackend
from allauth.account.models import EmailAddress
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


class AuthenticationBackend(AllauthAuthenticationBackend):
    def _authenticate_by_email(self, email: str, password: str) -> User | None:
        user = email_identity(email)
        return user if self._check_password(user, password) else None
