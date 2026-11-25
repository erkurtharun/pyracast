from ipaddress import ip_network
from pathlib import Path
from runpy import run_path

import pytest
from allauth.core.internal.ratelimit import consume
from django.conf import LazySettings
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory

from pyracast import proxy
from pyracast import settings as project_settings
from pyracast.accounts.checks import Network, check_authentication_origin, parse_networks
from pyracast.proxy import (
    CLIENT_IP_HEADER,
    ForwardedIdentity,
    InvalidPeerError,
    TrustedProxyHeaders,
    UnverifiedProxyHeadersError,
    resolve_forwarded_identity,
)

GATEWAY = "172.30.80.2"
GATEWAYS = (ip_network("172.30.80.2/32"),)
EDGES = (ip_network("173.245.48.0/20"), ip_network("2400:cb00::/32"))
CLIENT_META = "HTTP_X_PYRACAST_CLIENT_IP"


def meta(peer: str = GATEWAY, **headers: str) -> dict[str, str]:
    return {"REMOTE_ADDR": peer, **{f"HTTP_{name}": value for name, value in headers.items()}}


@pytest.mark.parametrize(
    ("request_meta", "gateways", "edges", "identity"),
    [
        # A peer that is no trusted gateway supplies no identity, whatever it sends.
        (
            meta("192.0.2.2", X_FORWARDED_PROTO="https", X_REAL_IP="127.0.0.1"),
            GATEWAYS,
            EDGES,
            ForwardedIdentity(peer="192.0.2.2"),
        ),
        (
            meta("173.245.48.1", X_FORWARDED_FOR="127.0.0.1", CF_IPCOUNTRY="TR"),
            GATEWAYS,
            EDGES,
            ForwardedIdentity(peer="173.245.48.1"),
        ),
        (
            meta(X_FORWARDED_PROTO="https", X_REAL_IP="127.0.0.1"),
            (),
            EDGES,
            ForwardedIdentity(peer=GATEWAY),
        ),
        (meta("::ffff:192.0.2.9"), GATEWAYS, (), ForwardedIdentity(peer="192.0.2.9")),
        # Without edge ranges the gateway names the client in X-Real-IP.
        (
            meta(X_FORWARDED_PROTO="https", X_REAL_IP="2001:db8::1", X_FORWARDED_FOR="1.1.1.1"),
            GATEWAYS,
            (),
            ForwardedIdentity(peer=GATEWAY, client="2001:db8::1", scheme="https"),
        ),
        (
            meta(X_FORWARDED_PROTO="http", X_REAL_IP="::ffff:198.51.100.7", CF_IPCOUNTRY="TR"),
            GATEWAYS,
            (),
            ForwardedIdentity(peer=GATEWAY, client="198.51.100.7", scheme="http"),
        ),
        # With edge ranges the client is the address the edge appended before the gateway.
        *[
            (
                meta(X_FORWARDED_PROTO="https", X_REAL_IP="173.245.48.1", X_FORWARDED_FOR=chain),
                GATEWAYS,
                EDGES,
                ForwardedIdentity(peer=GATEWAY, client=client, scheme="https"),
            )
            for chain, client in [
                ("198.51.100.7, 173.245.48.1", "198.51.100.7"),
                ("127.0.0.1, 198.51.100.7, 173.245.48.1", "198.51.100.7"),
                ("invalid, 2001:db8::7, 173.245.48.1", "2001:db8::7"),
                ("127.0.0.1, ::ffff:198.51.100.7, 173.245.48.1", "198.51.100.7"),
                ("127.0.0.1, 198.51.100.7, 2400:cb00::1", "198.51.100.7"),
                ("127.0.0.1, 173.245.49.1, 173.245.48.1", "173.245.49.1"),
                ("198.51.100.7", "198.51.100.7"),
            ]
        ],
        (
            meta(X_FORWARDED_PROTO="https", X_FORWARDED_FOR="198.51.100.7, 173.245.48.1"),
            GATEWAYS,
            EDGES,
            ForwardedIdentity(peer=GATEWAY, client="198.51.100.7", scheme="https"),
        ),
        # The country is the edge's only when the last hop is an edge, IPv6 edges included.
        (
            meta(
                X_FORWARDED_PROTO="https",
                X_FORWARDED_FOR="198.51.100.7, 2400:cb00::1",
                CF_IPCOUNTRY="TR",
            ),
            GATEWAYS,
            EDGES,
            ForwardedIdentity(peer=GATEWAY, client="198.51.100.7", scheme="https", country="TR"),
        ),
        (
            meta(X_FORWARDED_PROTO="https", X_FORWARDED_FOR="198.51.100.7", CF_IPCOUNTRY="TR"),
            GATEWAYS,
            EDGES,
            ForwardedIdentity(peer=GATEWAY, client="198.51.100.7", scheme="https"),
        ),
    ],
)
def test_the_resolver_decides_the_identity_from_the_trusted_chain(
    request_meta: dict[str, str],
    gateways: tuple[Network, ...],
    edges: tuple[Network, ...],
    identity: ForwardedIdentity,
) -> None:
    assert resolve_forwarded_identity(request_meta, gateways, edges) == identity


@pytest.mark.parametrize("peer", ["", "invalid", "192.0.2.1, 192.0.2.2", "10.0.0.1/24"])
def test_an_unreadable_socket_peer_is_refused(peer: str) -> None:
    with pytest.raises(InvalidPeerError):
        resolve_forwarded_identity(meta(peer), GATEWAYS, EDGES)
    with pytest.raises(InvalidPeerError):
        resolve_forwarded_identity({}, GATEWAYS, EDGES)


@pytest.mark.parametrize(
    ("headers", "edges"),
    [
        ({"X_FORWARDED_PROTO": "https,http", "X_REAL_IP": "192.0.2.1"}, ()),
        ({"X_REAL_IP": "192.0.2.1"}, ()),
        ({"X_FORWARDED_PROTO": "https", "X_REAL_IP": ""}, ()),
        ({"X_FORWARDED_PROTO": "https"}, ()),
        ({"X_FORWARDED_PROTO": "https", "X_REAL_IP": "192.0.2.1, 10.0.0.1"}, ()),
        ({"X_FORWARDED_PROTO": "https", "X_REAL_IP": "not-an-address"}, ()),
        ({"X_FORWARDED_FOR": "198.51.100.7, 173.245.48.1"}, EDGES),
        ({"X_FORWARDED_PROTO": "https"}, EDGES),
        ({"X_FORWARDED_PROTO": "https", "X_FORWARDED_FOR": ""}, EDGES),
        ({"X_FORWARDED_PROTO": "https", "X_FORWARDED_FOR": "invalid"}, EDGES),
        ({"X_FORWARDED_PROTO": "https", "X_FORWARDED_FOR": "198.51.100.7, invalid"}, EDGES),
        ({"X_FORWARDED_PROTO": "https", "X_FORWARDED_FOR": "173.245.48.1"}, EDGES),
        ({"X_FORWARDED_PROTO": "https", "X_FORWARDED_FOR": "invalid, 173.245.48.1"}, EDGES),
    ],
)
def test_a_trusted_gateway_with_unverifiable_headers_is_refused(
    headers: dict[str, str], edges: tuple[Network, ...]
) -> None:
    with pytest.raises(UnverifiedProxyHeadersError) as refused:
        resolve_forwarded_identity(meta(**headers), GATEWAYS, edges)
    assert refused.value.peer == GATEWAY


@pytest.mark.parametrize(
    ("values", "networks"),
    [
        ([], ()),
        (["172.30.80.2/32"], (ip_network("172.30.80.2/32"),)),
        (
            ["173.245.48.0/20,2400:cb00::/32", " 10.0.0.0/8 "],
            (ip_network("173.245.48.0/20"), ip_network("2400:cb00::/32"), ip_network("10.0.0.0/8")),
        ),
    ],
)
def test_network_settings_accept_comma_or_whitespace_separated_cidrs(
    values: list[str], networks: tuple[Network, ...]
) -> None:
    assert parse_networks("PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS", values) == networks


@pytest.mark.parametrize("value", ["10.0.0.1/24", "not-a-network", "10.0.0.0/33"])
def test_a_mistyped_network_names_its_setting(value: str) -> None:
    with pytest.raises(ImproperlyConfigured, match="PYRACAST_TRUSTED_PROXY_CIDRS"):
        parse_networks("PYRACAST_TRUSTED_PROXY_CIDRS", [value])


def proxy_response(request: HttpRequest) -> HttpResponse:
    return HttpResponse()


def trust(settings: LazySettings, *, edges: list[str] | None = None) -> None:
    settings.PYRACAST_TRUST_PROXY = True
    settings.PYRACAST_TRUSTED_PROXY_CIDRS = [f"{GATEWAY}/32"]
    settings.PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS = (
        ["173.245.48.0/20", "2400:cb00::/32"] if edges is None else edges
    )
    settings.SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    settings.ALLAUTH_TRUSTED_CLIENT_IP_HEADER = CLIENT_IP_HEADER


def test_the_middleware_hands_only_the_resolved_identity_to_the_application(
    settings: LazySettings,
) -> None:
    trust(settings)
    request = RequestFactory().get(
        "/",
        REMOTE_ADDR=GATEWAY,
        HTTP_FORWARDED="for=127.0.0.1;proto=https",
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_X_FORWARDED_FOR="127.0.0.1, 198.51.100.7, 173.245.48.1",
        HTTP_X_FORWARDED_HOST="attacker.example",
        HTTP_X_REAL_IP="173.245.48.1",
        HTTP_X_PYRACAST_CLIENT_IP="127.0.0.1",
        HTTP_CF_CONNECTING_IP="127.0.0.1",
        HTTP_CF_CONNECTING_IPV6="::1",
        HTTP_TRUE_CLIENT_IP="127.0.0.1",
        HTTP_CF_IPCOUNTRY="TR",
    )

    TrustedProxyHeaders(proxy_response)(request)

    forwarded = {
        name: value
        for name, value in request.META.items()
        if name.startswith(("HTTP_X_", "HTTP_FORWARDED", "HTTP_CF_", "HTTP_TRUE_"))
    }
    assert forwarded == {
        "HTTP_X_FORWARDED_PROTO": "https",
        CLIENT_META: "198.51.100.7",
        "HTTP_CF_IPCOUNTRY": "TR",
    }
    assert request.META["REMOTE_ADDR"] == GATEWAY
    assert request.is_secure()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("remote", ["192.0.2.2", "173.245.48.1"])
def test_an_untrusted_peer_cannot_supply_any_proxy_identity(
    settings: LazySettings, enabled: bool, remote: str
) -> None:
    trust(settings)
    settings.PYRACAST_TRUST_PROXY = enabled
    request = RequestFactory().get(
        "/",
        REMOTE_ADDR=remote,
        HTTP_FORWARDED="for=127.0.0.1;proto=https",
        HTTP_X_FORWARDED_FOR="127.0.0.1",
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_X_REAL_IP="127.0.0.1",
        HTTP_X_PYRACAST_CLIENT_IP="127.0.0.1",
        HTTP_CF_CONNECTING_IP="127.0.0.1",
        HTTP_CF_IPCOUNTRY="TR",
    )

    TrustedProxyHeaders(proxy_response)(request)

    assert not any(
        name.startswith(("HTTP_X_", "HTTP_FORWARDED", "HTTP_CF_")) for name in request.META
    )


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
@pytest.mark.parametrize(("enabled", "status"), [(False, 400), (True, 403)])
def test_an_untrusted_peer_with_trust_enabled_is_refused_by_the_authentication_limits(
    settings: LazySettings, enabled: bool, status: int
) -> None:
    trust(settings)
    if not enabled:
        settings.PYRACAST_TRUST_PROXY = False
        settings.ALLAUTH_TRUSTED_CLIENT_IP_HEADER = None

    response = Client(REMOTE_ADDR="192.0.2.2").post(
        "/api/auth/browser/v1/auth/login",
        {"email": "visitor@example.com", "password": "not-a-password"},
        content_type="application/json",
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_X_PYRACAST_CLIENT_IP="198.51.100.7",
        secure=True,
    )

    assert response.status_code == status


def test_a_trusted_gateway_without_verifiable_headers_answers_the_proxy_refusal(
    settings: LazySettings,
) -> None:
    trust(settings, edges=[])

    with pytest.raises(PermissionDenied, match="Something went wrong"):
        TrustedProxyHeaders(proxy_response)(
            RequestFactory().get(
                "/", REMOTE_ADDR=GATEWAY, HTTP_X_FORWARDED_PROTO="https", HTTP_X_REAL_IP="x"
            )
        )


def test_an_unreadable_socket_peer_is_denied(settings: LazySettings) -> None:
    trust(settings)

    with pytest.raises(PermissionDenied, match="Invalid client address"):
        TrustedProxyHeaders(proxy_response)(RequestFactory().get("/", REMOTE_ADDR="invalid"))


def test_proxy_trust_requires_explicit_gateway_networks(settings: LazySettings) -> None:
    trust(settings)
    settings.PYRACAST_TRUSTED_PROXY_CIDRS = []

    with pytest.raises(ImproperlyConfigured, match="explicit"):
        TrustedProxyHeaders(proxy_response)


def test_a_mistyped_edge_network_stops_the_middleware(settings: LazySettings) -> None:
    trust(settings, edges=["173.245.48.1/20"])

    with pytest.raises(ImproperlyConfigured, match="PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS"):
        TrustedProxyHeaders(proxy_response)


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://example.com/path",
        "https://x@example.com",
        "https:///",
        "https://[invalid",
        "https://example.com:invalid",
    ],
)
def test_production_rejects_unsafe_public_origins(settings: LazySettings, origin: str) -> None:
    settings.DEBUG = False
    settings.PYRACAST_PUBLIC_ORIGIN = origin
    with pytest.raises(ImproperlyConfigured, match="public origin"):
        TrustedProxyHeaders(proxy_response)
    assert [error.id for error in check_authentication_origin()] == ["accounts.E002"]


def test_development_accepts_local_http_origin(settings: LazySettings) -> None:
    settings.DEBUG = True
    settings.PYRACAST_PUBLIC_ORIGIN = "http://127.0.0.1:3000"
    TrustedProxyHeaders(proxy_response)
    assert check_authentication_origin() == []


@pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)
def test_cloudflare_visitors_keep_separate_rate_limits_and_cannot_forge_identity(
    settings: LazySettings,
) -> None:
    trust(settings)
    allowed = []
    for chain in (
        "127.0.0.1, 198.51.100.7, 173.245.48.1",
        "192.0.2.1, 198.51.100.7, 173.245.48.1",
        "127.0.0.1, 198.51.100.8, 173.245.48.1",
    ):
        request = RequestFactory().post(
            "/",
            REMOTE_ADDR=GATEWAY,
            HTTP_X_FORWARDED_PROTO="https",
            HTTP_X_REAL_IP="173.245.48.1",
            HTTP_X_FORWARDED_FOR=chain,
        )
        TrustedProxyHeaders(proxy_response)(request)
        allowed.append(
            bool(
                consume(request, app="allauth.account", action="login", config={"login": "1/m/ip"})
            )
        )
    assert allowed == [True, False, True]


def test_with_proxy_trust_authentication_limits_read_only_the_resolved_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYRACAST_TRUST_PROXY", "1")
    monkeypatch.setenv("PYRACAST_TRUSTED_PROXY_CIDRS", f"{GATEWAY}/32")
    monkeypatch.setenv("PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS", "173.245.48.0/20, 2400:cb00::/32")

    configuration = run_path(str(Path(project_settings.__file__)))

    assert configuration["ALLAUTH_TRUSTED_CLIENT_IP_HEADER"] == CLIENT_IP_HEADER
    assert configuration["PYRACAST_TRUSTED_PROXY_CIDRS"] == [f"{GATEWAY}/32"]
    assert configuration["PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS"] == [
        "173.245.48.0/20",
        "2400:cb00::/32",
    ]


def test_the_resolved_client_travels_in_a_header_only_the_middleware_writes() -> None:
    assert CLIENT_IP_HEADER == "X-Pyracast-Client-IP"
    assert proxy.CLIENT_IP_META == CLIENT_META
