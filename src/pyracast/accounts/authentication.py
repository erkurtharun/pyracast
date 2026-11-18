from collections.abc import Callable

from allauth.account.auth_backends import AuthenticationBackend as AllauthAuthenticationBackend
from allauth.account.models import EmailAddress
from allauth.core import context
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import User
from django.db.models import Exists, OuterRef

from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.accounts.authorization import requires_admin_mfa
from pyracast.accounts.processes import identifying, lock_account


def holders(email: str) -> tuple[list[User], list[User]]:
    # Accounts holding the address verified, and accounts whose only, primary address it is.
    others = EmailAddress.objects.filter(user_id=OuterRef("user_id")).exclude(pk=OuterRef("pk"))
    addresses = (
        EmailAddress.objects.filter(email__iexact=email)
        .select_related("user")
        .annotate(shared=Exists(others))
        .order_by("user_id")
    )
    verified = [address.user for address in addresses if address.verified]
    initial = [address.user for address in addresses if address.primary and not address.shared]
    return verified, initial


def identify(email: str, choose: Callable[[list[User], list[User]], User | None]) -> User | None:
    # In a request that starts a pending process, the chosen account's rows are locked and the
    # choice is made again from what was read under the lock.
    chosen = choose(*holders(email))
    if not identifying():
        return chosen
    while chosen is not None:
        lock_account(chosen.pk)
        current = choose(*holders(email))
        if current is not None and current.pk == chosen.pk:
            return current
        chosen = current
    return None


def _login_choice(verified: list[User], initial: list[User]) -> User | None:
    candidates = verified or initial
    return candidates[0] if len(candidates) == 1 else None


def _recovery_choice(verified: list[User], initial: list[User]) -> User | None:
    if verified:
        user = verified[0] if len(verified) == 1 else None
        return user if user is not None and user.is_active else None
    return next((user for user in initial if user.is_active), None)


def login_identity(email: str) -> User | None:
    return identify(email, _login_choice)


def recovery_identity(email: str) -> User | None:
    return identify(email, _recovery_choice)


def password_sign_in_refusal(user: User) -> str | None:
    if not user.is_active:
        return "inactive"
    if (
        requires_admin_mfa(user)
        and not Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).exists()
    ):
        return "mfa_not_provisioned"
    return None


class AuthenticationBackend(AllauthAuthenticationBackend):
    # A refused account must look like a wrong password to allauth: returning None keeps the
    # login_failed hit, while the base class would stash the user and answer "inactive".
    def _authenticate_by_email(self, email: str, password: str) -> User | None:
        user = login_identity(email)
        if user is None:
            return None
        self._did_check_password = True
        if not user.check_password(password):
            return None
        if (refusal := password_sign_in_refusal(user)) is not None:
            record_security_event(
                SecurityEvent.LOGIN_REFUSED,
                request=context.request,
                user_id=user.pk,
                reason=refusal,
            )
            return None
        return user
