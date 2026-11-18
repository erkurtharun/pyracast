from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network
from typing import NoReturn

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import HttpRequest, HttpResponse

from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.accounts.checks import validate_public_origin
from pyracast.accounts.security import normalize_client_ip
from pyracast.errors import ErrorCode, ForbiddenError

Network = IPv4Network | IPv6Network
# Allauth reads the client from this header (ALLAUTH_TRUSTED_CLIENT_IP_HEADER); only the
# middleware writes it, the gateway deletes it from every request.
CLIENT_IP_HEADER = "X-Pyracast-Client-IP"
CLIENT_IP_META = "HTTP_X_PYRACAST_CLIENT_IP"
FORWARDING_HEADERS = frozenset(
    {
        "HTTP_FORWARDED",
        "HTTP_X_REAL_IP",
        "HTTP_CF_CONNECTING_IP",
        "HTTP_CF_CONNECTING_IPV6",
        "HTTP_CF_IPCOUNTRY",
        "HTTP_TRUE_CLIENT_IP",
        CLIENT_IP_META,
    }
)


@dataclass(frozen=True)
class ForwardedIdentity:
    peer: str
    client: str | None = None
    scheme: str | None = None
    country: str | None = None


class InvalidPeerError(ValueError):
    pass


class UnverifiedProxyHeadersError(ValueError):
    def __init__(self, peer: str) -> None:
        super().__init__(peer)
        self.peer = peer


def parse_networks(name: str, values: Iterable[str]) -> tuple[Network, ...]:
    entries = [entry for value in values for entry in value.replace(",", " ").split()]
    try:
        return tuple(ip_network(entry) for entry in entries)
    except ValueError as error:
        raise ImproperlyConfigured(f"{name} holds an invalid network: {error}") from None


def _address(value: object) -> IPv4Address | IPv6Address:
    if not isinstance(value, str):
        raise ValueError("Missing address.")
    return ip_address(normalize_client_ip(value))


def _within(address: IPv4Address | IPv6Address, networks: Iterable[Network]) -> bool:
    return any(address in network for network in networks)


def _edge_client(forwarded_for: object, edges: tuple[Network, ...]) -> str:
    if not isinstance(forwarded_for, str) or not forwarded_for:
        raise ValueError("Missing forwarded client address.")
    prefix, separator, peer = forwarded_for.rpartition(",")
    address = _address(peer.strip())
    if not _within(address, edges):
        return str(address)
    if not separator:
        raise ValueError("Forwarded chain contains no client address.")
    # One edge appends its client, then the gateway appends that edge. Another edge-range address
    # before it is the client itself, never skipped.
    return str(_address(prefix.rsplit(",", 1)[-1].strip()))


def resolve_forwarded_identity(
    meta: Mapping[str, object], gateways: tuple[Network, ...], edges: tuple[Network, ...]
) -> ForwardedIdentity:
    try:
        peer = _address(meta.get("REMOTE_ADDR"))
    except ValueError:
        raise InvalidPeerError from None
    if not _within(peer, gateways):
        return ForwardedIdentity(peer=str(peer))
    scheme = meta.get("HTTP_X_FORWARDED_PROTO")
    forwarded_for = meta.get("HTTP_X_FORWARDED_FOR")
    try:
        if scheme not in {"http", "https"}:
            raise ValueError("Unverifiable scheme.")
        client = (
            _edge_client(forwarded_for, edges)
            if edges
            else str(_address(meta.get("HTTP_X_REAL_IP")))
        )
    except ValueError:
        raise UnverifiedProxyHeadersError(str(peer)) from None
    country = meta.get("HTTP_CF_IPCOUNTRY")
    last_hop = str(forwarded_for).rsplit(",", 1)[-1].strip()
    from_edge = bool(edges) and _within(_address(last_hop), edges)
    return ForwardedIdentity(
        peer=str(peer),
        client=client,
        scheme=str(scheme),
        country=country if from_edge and isinstance(country, str) and country else None,
    )


class TrustedProxyHeaders:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        validate_public_origin()
        self.gateways = (
            parse_networks("PYRACAST_TRUSTED_PROXY_CIDRS", settings.PYRACAST_TRUSTED_PROXY_CIDRS)
            if settings.PYRACAST_TRUST_PROXY
            else ()
        )
        if settings.PYRACAST_TRUST_PROXY and not self.gateways:
            raise ImproperlyConfigured(
                "PYRACAST_TRUST_PROXY requires explicit PYRACAST_TRUSTED_PROXY_CIDRS."
            )
        self.edges = parse_networks(
            "PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS", settings.PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS
        )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        meta = request.META
        try:
            identity = resolve_forwarded_identity(meta, self.gateways, self.edges)
        except InvalidPeerError:
            raise PermissionDenied("Invalid client address.") from None
        except UnverifiedProxyHeadersError as refused:
            self._refuse(refused.peer)
        for name in [
            name
            for name in meta
            if name.startswith("HTTP_X_FORWARDED_") or name in FORWARDING_HEADERS
        ]:
            del meta[name]
        meta["REMOTE_ADDR"] = identity.peer
        if identity.client is not None:
            meta["HTTP_X_FORWARDED_PROTO"] = identity.scheme
            meta[CLIENT_IP_META] = identity.client
        if identity.country is not None:
            meta["HTTP_CF_IPCOUNTRY"] = identity.country
        return self.get_response(request)

    @staticmethod
    def _refuse(peer: str) -> NoReturn:
        # The allauth adapter cannot resolve the client here: it would refuse the same headers.
        record_security_event(SecurityEvent.INVALID_PROXY_HEADERS, client_ip=peer)
        raise ForbiddenError(ErrorCode.INVALID_PROXY_HEADERS)
