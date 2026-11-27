import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum, auto
from ipaddress import IPv6Address, ip_address
from typing import Any

from allauth.account import app_settings as account_settings
from allauth.account.internal.flows.login import get_authentication_records
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.http import HttpRequest


class Factor(StrEnum):
    # The values are allauth's names of the methods in the session's authentication records.
    MFA = auto()
    PASSWORD = auto()
    CODE = auto()


def normalize_client_ip(value: str) -> str:
    address = ip_address(value)
    if isinstance(address, IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


def strongest_factor(user: AbstractBaseUser) -> Factor:
    if Authenticator.objects.filter(user_id=user.pk).exists():
        return Factor.MFA
    return Factor.PASSWORD if user.has_usable_password() else Factor.CODE


@dataclass(frozen=True, slots=True)
class ProofWindow:
    seconds: Callable[[], int]
    sign_in_counts: bool


# A sensitive change needs a reauthentication inside allauth's window; a sign-in is not one.
CONFIRMATION = ProofWindow(lambda: account_settings.REAUTHENTICATION_TIMEOUT, sign_in_counts=False)
CONSOLE_PROOF = ProofWindow(lambda: settings.PYRACAST_CONSOLE_PROOF_SECONDS, sign_in_counts=True)


def _inside(record: dict[str, Any], window: ProofWindow, now: float) -> bool:
    at = record.get("at")
    return (
        (window.sign_in_counts or bool(record.get("reauthenticated")))
        and isinstance(at, int | float)
        and 0 <= now - at < window.seconds()
    )


def has_mfa_evidence(request: HttpRequest, *, within: ProofWindow | None = None) -> bool:
    user = request.user
    if not user.is_authenticated or not user.is_active:
        return False
    now = time.time()
    allowed = (
        (Authenticator.Type.TOTP, Authenticator.Type.RECOVERY_CODES)
        if user.is_staff or user.is_superuser
        else settings.MFA_SUPPORTED_TYPES
    )
    authenticator_ids = [
        record.get("id")
        for record in get_authentication_records(request, user)
        if record.get("method") == Factor.MFA
        and record.get("type") in allowed
        and (within is None or _inside(record, within, now))
    ]
    return bool(
        authenticator_ids
        and Authenticator.objects.filter(
            user=user, type__in=allowed, pk__in=authenticator_ids
        ).exists()
    )
