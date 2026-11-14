import math
import time
from collections.abc import Callable
from ipaddress import ip_address, ip_network
from typing import cast

from django.conf import settings
from django.contrib.auth import logout
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import HttpRequest, HttpResponse

from pyracast.accounts.checks import validate_public_origin
from pyracast.accounts.security import normalize_client_ip


class TrustedProxyHeaders:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        validate_public_origin()
        self.networks = [ip_network(value) for value in settings.PYRACAST_TRUSTED_PROXY_CIDRS]
        self.upstream_networks = [
            ip_network(value) for value in settings.PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS
        ]
        if settings.PYRACAST_TRUST_PROXY and not self.networks:
            raise ImproperlyConfigured(
                "PYRACAST_TRUST_PROXY requires explicit PYRACAST_TRUSTED_PROXY_CIDRS."
            )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        try:
            remote = ip_address(normalize_client_ip(request.META.get("REMOTE_ADDR", "")))
        except ValueError:
            raise PermissionDenied("Invalid client address.") from None
        request.META["REMOTE_ADDR"] = str(remote)
        trusted = settings.PYRACAST_TRUST_PROXY and any(
            remote in network for network in self.networks
        )
        forwarded_proto = request.META.get("HTTP_X_FORWARDED_PROTO")
        forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        client_ip = request.META.get("HTTP_X_REAL_IP")
        country = request.META.pop("HTTP_CF_IPCOUNTRY", None)
        for name in tuple(request.META):
            if name.startswith("HTTP_X_FORWARDED_") or name in {
                "HTTP_FORWARDED",
                "HTTP_X_REAL_IP",
                "HTTP_CF_CONNECTING_IP",
                "HTTP_CF_CONNECTING_IPV6",
                "HTTP_TRUE_CLIENT_IP",
            }:
                del request.META[name]
        if trusted:
            if forwarded_proto not in {"http", "https"} or not client_ip:
                raise PermissionDenied("Invalid trusted proxy headers.")
            try:
                client_ip = (
                    self._forwarded_client_ip(forwarded_for)
                    if self.upstream_networks
                    else normalize_client_ip(client_ip)
                )
            except ValueError:
                raise PermissionDenied("Invalid trusted proxy headers.") from None
            request.META["HTTP_X_FORWARDED_PROTO"] = forwarded_proto
            request.META["HTTP_X_REAL_IP"] = client_ip
            if country and self.upstream_networks and forwarded_for:
                upstream = ip_address(normalize_client_ip(forwarded_for.rsplit(",", 1)[-1].strip()))
                if any(upstream in network for network in self.upstream_networks):
                    request.META["HTTP_CF_IPCOUNTRY"] = country
        return self.get_response(request)

    def _forwarded_client_ip(self, forwarded_for: str | None) -> str:
        if not forwarded_for:
            raise ValueError("Missing forwarded client address.")
        prefix, separator, peer = forwarded_for.rpartition(",")
        address = ip_address(normalize_client_ip(peer.strip()))
        if not any(address in network for network in self.upstream_networks):
            return str(address)
        if not separator:
            raise ValueError("Forwarded chain contains no client address.")
        # One external proxy appends its client, then the gateway appends that proxy.
        # Do not skip another trusted-range address: the client itself may use that range.
        return normalize_client_ip(prefix.rsplit(",", 1)[-1].strip())


class PrivilegedAuthenticatorEnrollment:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = request.user
        if (
            request.path.rstrip("/") == "/api/auth/browser/v1/account/authenticators/totp"
            and request.method in {"GET", "POST"}
            and user.is_authenticated
            and (user.is_staff or user.is_superuser)
        ):
            raise PermissionDenied(
                "Administrator authenticators require trusted operator provisioning."
            )
        return self.get_response(request)


class SessionLifetime:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            now = time.time()
            authenticated_at = request.session.get("authenticated_at")
            last_activity_at = request.session.get("last_activity_at")
            if self._expired(authenticated_at, now, settings.SESSION_COOKIE_AGE) or self._expired(
                last_activity_at, now, settings.PYRACAST_SESSION_IDLE_SECONDS
            ):
                logout(request)
            elif now - cast(float, last_activity_at) >= 60:
                request.session["last_activity_at"] = now
        return self.get_response(request)

    @staticmethod
    def _expired(timestamp: object, now: float, lifetime: int) -> bool:
        return (
            not isinstance(timestamp, int | float)
            or not math.isfinite(timestamp)
            or timestamp > now
            or now - timestamp >= lifetime
        )
