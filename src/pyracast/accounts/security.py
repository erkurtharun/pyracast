import time
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


def _recent(record: dict[str, Any], now: float) -> bool:
    at = record.get("at")
    return (
        bool(record.get("reauthenticated"))
        and isinstance(at, int | float)
        and 0 <= now - at < account_settings.REAUTHENTICATION_TIMEOUT
    )


def has_mfa_evidence(request: HttpRequest, *, recent: bool = False) -> bool:
    # Recent evidence is a reauthentication by a still-enrolled authenticator within the one
    # reauthentication window (ACCOUNT_REAUTHENTICATION_TIMEOUT); a sign-in never counts as one.
    user = request.user
    if not user.is_authenticated or not user.is_active:
        return False
    now = time.time()
    authenticator_ids = [
        record.get("id")
        for record in get_authentication_records(request, user)
        if record.get("method") == Factor.MFA
        and record.get("type") in settings.MFA_SUPPORTED_TYPES
        and (not recent or _recent(record, now))
    ]
    return bool(
        authenticator_ids
        and Authenticator.objects.filter(user=user, pk__in=authenticator_ids).exists()
    )
