import time
from ipaddress import IPv6Address, ip_address
from typing import Any

from allauth.account.internal.flows.login import get_authentication_records
from allauth.mfa.models import Authenticator
from django.http import HttpRequest


def normalize_client_ip(value: str) -> str:
    address = ip_address(value)
    if isinstance(address, IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


def start_user_session(*, request: HttpRequest, **kwargs: Any) -> None:
    now = time.time()
    request.session["authenticated_at"] = now
    request.session["last_activity_at"] = now


def has_verified_mfa(request: HttpRequest) -> bool:
    user = request.user
    if not user.is_authenticated or not user.is_active:
        return False
    records = get_authentication_records(request, user)
    authenticator_ids = [
        record.get("id")
        for record in records
        if record.get("method") == "mfa" and record.get("type") in {"totp", "recovery_codes"}
    ]
    return bool(
        authenticator_ids
        and Authenticator.objects.filter(user=user, pk__in=authenticator_ids).exists()
    )
